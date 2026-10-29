# SPDX-License-Identifier: AGPL-3.0-only
"""L161f — test selection from the measured map.

`scripts/test-plan.py plan` classes every test module once per CI run (P0
changed test, P1 impacted by changed blocks, files, directories or tables,
P2 recent failure, P3 the rest) and writes the parts; every part runs from
that plan (`scripts/test-parts.py run --plan`) and says when its impacted
modules are done (provisional green); `leaks` counts the failures of
modules the map did not select. No database needed.
"""
from __future__ import annotations

import ast
import contextlib
import datetime
import importlib.util
import io
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parent.parent


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


plan = _load("test_plan_l161f", "test-plan.py")
parts = _load("test_parts_l161f", "test-parts.py")
sys.path.insert(0, str(REPO / "scripts"))
import map as map_mod  # noqa: E402  scripts/map.py

NOW = datetime.datetime(2026, 10, 11, 12, 0, tzinfo=datetime.timezone.utc)

CODE_V1 = textwrap.dedent('''\
    """Module."""
    LIMIT = 3


    def used(x):
        return x + 1


    def untested(x):
        return x * 2


    class Box:
        size = 1

        def get(self):
            return self.size
''')


def _git(repo: pathlib.Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                          text=True).stdout.strip()


class _Repo:
    """A throwaway git repository, its first commit standing for the map commit."""

    def __init__(self) -> None:
        self.path = pathlib.Path(tempfile.mkdtemp())
        _git(self.path, "init", "-q")
        for k, v in (("user.name", "t"), ("user.email", "t@t.invalid"),
                     ("commit.gpgsign", "false")):
            _git(self.path, "config", k, v)
        self.write("src/ameesh/code.py", CODE_V1)
        self.write("src/ameesh/migrations/0001_init.sql", "create table things (id int);\n")
        self.write("deploy/sql/role.sql", "grant select on things to r;\n")
        self.write("data/conf.json", "{}\n")
        self.write("docs/guide.md", "# Guide\n")
        self.write("deploy/install.sh", "echo\n")
        self.base = self.commit()
        self.code = CODE_V1  # src/ameesh/code.py at the map commit

    def write(self, rel: str, text: str) -> None:
        f = self.path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")

    def remove(self, rel: str) -> None:
        (self.path / rel).unlink()

    def commit(self) -> str:
        _git(self.path, "add", "-A")
        _git(self.path, "commit", "-q", "-m", "c", "--allow-empty")
        return _git(self.path, "rev-parse", "HEAD")

    def card(self) -> dict:
        """A map of the first commit: who executed, read, listed, touched what."""
        infos, _ = map_mod.blocks(self.code)
        users = {"<module>": ["test_a", "test_b", "test_c"], "used": ["test_a"],
                 "untested": [], "Box": ["test_b"], "Box.get": ["test_b"]}
        return {
            "schema": map_mod.SCHEMA, "commit": self.base, "genere": NOW.isoformat(),
            "modules": {m: {"durees": {"psycopg": 1.0}, "echecs": []}
                        for m in ("test_a", "test_b", "test_c", "test_db", "test_old")},
            "blocs": {"src/ameesh/code.py": {n: {**i, "modules": users.get(n, [])}
                                             for n, i in infos.items()}},
            "donnees": {"data/conf.json": ["test_c"], "deploy/sql/role.sql": ["test_cluster"]},
            "dossiers": {"data": ["test_lister"], "src/ameesh/migrations": ["test_migrations"]},
            "tables": {"things": ["test_db"], "others": ["test_other"]},
            "migrations": {"src/ameesh/migrations/0001_init.sql": ["things"]},
        }

    def judge(self, modules=("test_a", "test_b", "test_c", "test_db")) -> "plan.Impact":
        changes = plan.changed_files(self.base, "HEAD", self.path)
        return plan.judge(self.card(), changes, list(modules), "HEAD", self.path)

    def close(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


class JudgeTest(unittest.TestCase):
    def setUp(self):
        self.repo = _Repo()
        self.addCleanup(self.repo.close)

    def test_changed_function_impacts_its_tests_only(self):
        self.repo.write("src/ameesh/code.py", CODE_V1.replace("x + 1", "x + 2"))
        self.repo.commit()
        impact = self.repo.judge()
        self.assertEqual(impact.p1, {"test_a": "src/ameesh/code.py::used"})
        self.assertEqual(impact.unsure, [])
        self.assertEqual(impact.untested, [])

    def test_changed_lines_without_a_test_run_their_importers(self):
        # #1339 : un bloc modifié qu'aucun test n'exécute ne force plus la
        # suite complète : les tests qui importent son fichier le couvrent et
        # rejoignent les impactés ; le bloc reste listé (lignes sans test).
        self.repo.write("src/ameesh/code.py", CODE_V1.replace("x * 2", "x * 3"))
        self.repo.commit()
        impact = self.repo.judge()
        self.assertEqual(impact.p1, {m: "src/ameesh/code.py (imported)"
                                     for m in ("test_a", "test_b", "test_c")})
        self.assertEqual(impact.untested, [{"file": "src/ameesh/code.py", "block": "untested",
                                            "lines": [10, 10]}])
        self.assertEqual(impact.uncovered, [])

    def test_inherited_changed_block_is_not_a_line_of_the_branch(self):
        # #1909 : la cible a changé `untested` après la carte (x*2 -> x*4) ; la
        # branche ne change que `used`. L'empreinte du bloc est la même à la
        # base de fusion et à HEAD : il n'est pas une ligne de la branche, donc
        # pas listé — mais la sélection garde le doute de la carte et ses
        # importateurs (le bloc n'a aucune mesure).
        inherited = CODE_V1.replace("x * 2", "x * 4")
        self.repo.write("src/ameesh/code.py", inherited)
        base = self.repo.commit()
        self.repo.write("src/ameesh/code.py", inherited.replace("x + 1", "x + 2"))
        self.repo.commit()
        impact = plan.judge(self.repo.card(),
                            plan.changed_files(self.repo.base, "HEAD", self.repo.path),
                            ["test_a", "test_b", "test_c", "test_db"], "HEAD",
                            self.repo.path, own={"src/ameesh/code.py"}, own_base=base)
        self.assertEqual(impact.untested, [])
        self.assertEqual(impact.importers,
                         {"src/ameesh/code.py": ["test_a", "test_b", "test_c"]})

    def test_block_changed_by_the_branch_is_listed(self):
        # #1909 : le bloc sans test que la branche change elle-même reste
        # listé (la cible l'avait changé après la carte, la branche le change
        # encore : son empreinte diffère de la base comme de la carte).
        self.repo.write("src/ameesh/code.py", CODE_V1.replace("x * 2", "x * 4"))
        base = self.repo.commit()
        self.repo.write("src/ameesh/code.py", CODE_V1.replace("x * 2", "x * 3"))
        self.repo.commit()
        impact = plan.judge(self.repo.card(),
                            plan.changed_files(self.repo.base, "HEAD", self.repo.path),
                            ["test_a", "test_b", "test_c", "test_db"], "HEAD",
                            self.repo.path, own={"src/ameesh/code.py"}, own_base=base)
        self.assertEqual([u["block"] for u in impact.untested], ["untested"])

    def test_changed_block_without_an_own_range_stays_listed(self):
        # #1909 : sans range propre lisible (`own=None`), le rapport reste
        # conservateur : le bloc que la cible a changé après la carte est listé
        # comme avant.
        self.repo.write("src/ameesh/code.py", CODE_V1.replace("x * 2", "x * 4"))
        self.repo.commit()
        impact = self.repo.judge()
        self.assertEqual([u["block"] for u in impact.untested], ["untested"])

    def test_module_level_change_impacts_every_importer(self):
        self.repo.write("src/ameesh/code.py", CODE_V1.replace("LIMIT = 3", "LIMIT = 4"))
        self.repo.commit()
        self.assertEqual(set(self.repo.judge().p1), {"test_a", "test_b", "test_c"})

    def test_new_plain_function_found_through_its_caller(self):
        code = CODE_V1.replace("    return x + 1\n", "    return helper(x)\n") \
            + "\n\ndef helper(x):\n    return x + 1\n"
        self.repo.write("src/ameesh/code.py", code)
        self.repo.commit()
        impact = self.repo.judge()
        # l'appelant modifié nomme test_a ; les autres tests qui chargent le
        # fichier restent impactés (relecture Codex B1 : un nom calculé peut
        # atteindre la fonction nouvelle)
        self.assertEqual(impact.p1, {"test_a": "src/ameesh/code.py::used",
                                     "test_b": "src/ameesh/code.py (imported)",
                                     "test_c": "src/ameesh/code.py (imported)"})
        self.assertEqual(impact.unknown_blocks, ["src/ameesh/code.py::helper"])
        self.assertEqual(impact.importers,
                         {"src/ameesh/code.py": ["test_a", "test_b", "test_c"]})

    def test_new_function_keeps_the_importers_of_its_known_file(self):
        # #1339, relecture Codex (B1) : une fonction publique nouvelle n'a aucun
        # appelant mesuré ; un test existant qui parcourt le fichier (`dir()`
        # puis `getattr`) peut l'exécuter, donc tous les tests qui ont chargé le
        # fichier restent impactés — sinon le plan se déclare sûr et les omet.
        self.repo.write("src/ameesh/code.py",
                        CODE_V1 + "\n\ndef added(x):\n    return x - 1\n")
        self.repo.commit()
        impact = self.repo.judge()
        self.assertEqual(impact.unknown_blocks, ["src/ameesh/code.py::added"])
        self.assertEqual(impact.uncovered, [])
        self.assertEqual(impact.p1, {m: "src/ameesh/code.py (imported)"
                                     for m in ("test_a", "test_b", "test_c")})
        self.assertEqual(impact.importers,
                         {"src/ameesh/code.py": ["test_a", "test_b", "test_c"]})

    def test_new_function_that_counts_for_its_module(self):
        cases = {
            "decorated": "\n\n@register\ndef extra():\n    pass\n",
            "dunder": "\n\ndef __getattr__(name):\n    pass\n",
            "rebinds a name": "\n\ndef LIMIT():\n    pass\n",
            "new class": "\n\nclass Extra:\n    pass\n",
        }
        for why, extra in cases.items():
            with self.subTest(why):
                self.repo.write("src/ameesh/code.py", CODE_V1 + extra)
                self.repo.commit()
                self.assertIn("test_c", self.repo.judge().p1, why)

    def test_new_method_always_counts_for_its_class(self):
        # L161f review (a): a method may override a base method or be dispatched
        # by name (do_DELETE of BaseHTTPRequestHandler), even with a new name
        self.repo.write("src/ameesh/code.py", CODE_V1 + "\n    def do_DELETE(self):\n        pass\n")
        self.repo.commit()
        self.assertIn("test_b", self.repo.judge().p1)

    def test_new_function_shadowing_a_builtin_counts(self):
        self.repo.write("src/ameesh/code.py", CODE_V1 + "\n\ndef list():\n    pass\n")
        self.repo.commit()
        self.assertIn("test_c", self.repo.judge().p1)

    def test_nothing_taken_out_of_a_dynamic_module(self):
        def judged(line: str) -> "plan.Impact":
            self.repo.code = CODE_V1 + line + "\n"  # the map knows the line
            self.repo.write("src/ameesh/code.py", self.repo.code)
            self.repo.base = self.repo.commit()
            self.repo.write("src/ameesh/code.py", self.repo.code + "\n\ndef _extra():\n    pass\n")
            self.repo.commit()
            return self.repo.judge()

        for why, line in {"getattr with a computed name": "f = getattr(Box, '_' + 'x', None)",
                          "globals()": "g = globals()",
                          "import *": "from os.path import *"}.items():
            with self.subTest(why):
                impact = judged(line)
                self.assertIn("test_c", impact.p1, why)
                # le module compte comme modifié : le bloc <module> a changé
                self.assertEqual(impact.p1["test_c"], "src/ameesh/code.py::<module>", why)
        # a literal getattr name stays static: the new function is taken out,
        # so no block of the file changes; its importers stay impacted all the
        # same (#1339, relecture Codex B1), as "imported" coverage
        impact = judged("f = getattr(Box, 'size')")
        self.assertEqual(impact.p1, {m: "src/ameesh/code.py (imported)"
                                     for m in ("test_a", "test_b", "test_c")})

    def test_removed_file_impacts_all_its_tests(self):
        self.repo.remove("src/ameesh/code.py")
        self.repo.commit()
        self.assertEqual(set(self.repo.judge().p1), {"test_a", "test_b", "test_c"})

    def test_syntax_error_is_unsure(self):
        self.repo.write("src/ameesh/code.py", "def broken(:\n")
        self.repo.commit()
        impact = self.repo.judge()
        self.assertEqual(impact.textual, ["src/ameesh/code.py"])
        self.assertTrue(impact.unsure)

    def test_data_files_and_directories(self):
        self.repo.write("data/conf.json", '{"a": 1}\n')
        self.repo.write("data/new.json", "{}\n")
        self.repo.write("docs/guide.md", "# Guide, again\n")
        self.repo.commit()
        impact = self.repo.judge()
        self.assertEqual(impact.p1, {"test_c": "data/conf.json",
                                     "test_lister": "data/new.json (added to data)"})
        self.assertEqual(impact.unsure, [])  # a documentation file no test reads

    def test_new_directory_reaches_the_listed_ancestor(self):
        self.repo.write("data/sub/deeper/x.json", "{}\n")
        self.repo.commit()
        self.assertEqual(self.repo.judge().p1, {"test_lister": "data/sub/deeper/x.json "
                                                               "(added to data)"})

    def test_file_no_test_reads_is_unsure(self):
        self.repo.write("deploy/install.sh", "echo changed\n")
        self.repo.commit()
        impact = self.repo.judge()
        self.assertEqual(impact.p1, {})
        self.assertEqual(impact.textual, ["deploy/install.sh"])
        self.assertTrue(impact.unsure)

    def test_test_modules_and_foundation(self):
        self.repo.write("tests/test_a.py", "x = 1\n")
        self.repo.write("tests/support.py", "x = 1\n")
        self.repo.write("scripts/tool.py", "x = 1\n")
        self.repo.commit()
        impact = self.repo.judge(("test_a", "test_new"))
        self.assertEqual(impact.p0, {"test_a": "tests/test_a.py",
                                     "test_new": "unknown to the map"})
        self.assertEqual(impact.foundation, ["tests/support.py"])
        self.assertEqual(sorted(impact.textual), ["scripts/tool.py", "tests/support.py"])
        self.assertEqual(len(impact.unsure), 2)

    def test_python_outside_socle_changed_by_target_is_not_a_doubt(self):
        # #3800 : la cible a ajouté deploy/mise-a-jour/canari-handoff.py après
        # la carte ; la branche ne le touche pas. Son range propre (`own`) ne
        # le contient pas, donc le fichier ne doit plus rendre la sélection
        # « non sûre » (sinon tout lot gelé retombait sur la suite complète).
        self.repo.write("deploy/mise-a-jour/canari-handoff.py", "print(1)\n")
        target = self.repo.commit()
        self.repo.write("src/ameesh/code.py", CODE_V1.replace("x + 1", "x + 2"))
        self.repo.commit()
        impact = plan.judge(
            self.repo.card(),
            plan.changed_files(self.repo.base, "HEAD", self.repo.path),
            ["test_a", "test_b", "test_c", "test_db"], "HEAD", self.repo.path,
            own={"src/ameesh/code.py"}, own_base=target)
        self.assertEqual(impact.unsure, [])
        self.assertEqual(impact.p1, {"test_a": "src/ameesh/code.py::used"})

    def test_python_outside_socle_changed_by_the_branch_is_a_doubt(self):
        # #3800 : le repli ne couvre que les changements de la cible ; un
        # fichier hors socle que la branche change elle-même reste un doute.
        self.repo.write("deploy/mise-a-jour/canari-handoff.py", "print(1)\n")
        self.repo.commit()
        impact = self.repo.judge()
        self.assertIn("deploy/mise-a-jour/canari-handoff.py: Python outside "
                      "src/ameesh/ is not measured", impact.unsure)

    def test_python_outside_socle_without_an_own_range_is_conservative(self):
        # #3800 : `own` inconnu (range illisible) garde le doute, comme pour
        # le socle et les données.
        self.repo.write("deploy/mise-a-jour/canari-handoff.py", "print(1)\n")
        self.repo.commit()
        impact = plan.judge(
            self.repo.card(),
            plan.changed_files(self.repo.base, "HEAD", self.repo.path),
            ["test_a", "test_b", "test_c", "test_db"], "HEAD", self.repo.path,
            own=None)
        self.assertTrue(any("canari-handoff.py" in reason
                            for reason in impact.unsure))

    def test_new_additive_migration(self):
        self.repo.write("src/ameesh/migrations/0002_more.sql",
                        "alter table things add column name text;\n")
        self.repo.commit()
        impact = self.repo.judge()
        self.assertEqual(set(impact.p1), {"test_migrations", "test_cluster", "test_db"})
        self.assertEqual(impact.p1["test_db"],
                         "src/ameesh/migrations/0002_more.sql (table things)")
        self.assertEqual(impact.unsure, [])

    def test_migration_not_additive_or_rewritten(self):
        self.repo.write("src/ameesh/migrations/0002_more.sql",
                        "alter table things drop column id;\n")
        self.repo.commit()
        self.assertEqual({"test_db", "test_other"} - set(self.repo.judge().p1), set())
        self.repo.write("src/ameesh/migrations/0001_init.sql", "create table things (id bigint);\n")
        self.repo.commit()
        impact = self.repo.judge()
        self.assertEqual({"test_db", "test_other"} - set(impact.p1), set())
        self.assertTrue(impact.unsure)


class MapTest(unittest.TestCase):
    def setUp(self):
        self.repo = _Repo()
        self.addCleanup(self.repo.close)

    def _classify(self) -> dict:
        mods = ["test_a", "test_b", "test_c", "test_db"]
        with mock.patch.object(plan, "ALWAYS_FILE", self.repo.path / "none.txt"):
            return plan.classify(mods, self.repo.card(), NOW, head="HEAD", repo=self.repo.path)

    def test_phase2_safe_with_changed_lines_without_a_test(self):
        # L161f review (4), #1339 : le bloc modifié sans test est couvert par
        # les tests qui importent son fichier (test_a, test_b, test_c) : la
        # sélection impactés+contrat suffit, le bloc reste listé.
        self.repo.write("src/ameesh/code.py", CODE_V1.replace("x * 2", "x * 3"))
        self.repo.commit()
        out = self._classify()
        self.assertTrue(out["sure"])
        self.assertEqual([u["block"] for u in out["untested"]], ["untested"])
        self.assertTrue(out["phase2_safe"], out["phase2_why"])
        self.assertEqual(set(out["importers"]["src/ameesh/code.py"]),
                         {"test_a", "test_b", "test_c"})
        self.assertTrue(all(out["classes"][m][0] == "P1" for m in ("test_a", "test_b", "test_c")))
        self.repo.write("src/ameesh/code.py", CODE_V1.replace("x + 1", "x + 2"))
        self.repo.commit()
        self.assertTrue(self._classify()["phase2_safe"])

    def test_plan_reports_only_the_block_the_branch_changed(self):
        # #1909 : `classify` lit la base de la branche (`base_fallback`) pour
        # son range propre. La cible a changé `untested` après la carte, la
        # branche change `used` : le plan ne liste pas le bloc hérité, et la
        # sélection garde ses importateurs (la carte est en retard sur lui).
        inherited = CODE_V1.replace("x * 2", "x * 4")
        self.repo.write("src/ameesh/code.py", inherited)
        base = self.repo.commit()
        self.repo.write("src/ameesh/code.py", inherited.replace("x + 1", "x + 2"))
        self.repo.commit()
        mods = ["test_a", "test_b", "test_c", "test_db"]
        with mock.patch.object(plan, "ALWAYS_FILE", self.repo.path / "none.txt"):
            out = plan.classify(mods, self.repo.card(), NOW, base_fallback=base,
                                head="HEAD", repo=self.repo.path)
        self.assertTrue(out["sure"], out["unsure"])
        self.assertEqual(out["untested"], [])
        self.assertEqual(set(out["importers"]["src/ameesh/code.py"]),
                         {"test_a", "test_b", "test_c"})
        self.assertEqual(out["classes"]["test_a"][0], "P1")

    def test_phase2_safe_with_a_new_plain_function(self):
        # #1339 : une fonction simple nouvelle est couverte par son appelant
        # modifié (test_a) ; tous les tests qui chargent le fichier restent
        # impactés eux aussi (relecture Codex B1 : un nom calculé peut
        # l'atteindre), donc la suite complète n'est pas forcée.
        code = CODE_V1.replace("    return x + 1\n", "    return helper(x)\n") \
            + "\n\ndef helper(x):\n    return x + 1\n"
        self.repo.write("src/ameesh/code.py", code)
        self.repo.commit()
        out = self._classify()
        self.assertIn("src/ameesh/code.py::helper", out["unknown_blocks"])
        self.assertTrue(out["phase2_safe"], out["phase2_why"])
        self.assertEqual(out["importers"]["src/ameesh/code.py"],
                         ["test_a", "test_b", "test_c"])
        self.assertEqual(out["classes"]["test_a"], ["P1", "src/ameesh/code.py::used"])
        self.assertTrue(all(out["classes"][m][0] == "P1"
                            for m in ("test_a", "test_b", "test_c")))

    def test_phase2_safe_with_a_new_function_and_no_caller(self):
        # #1339, relecture Codex (B1) : la fonction nouvelle n'a aucun appelant
        # mesuré ; le test réflexif qui parcourt le fichier est pourtant
        # impacté, donc il doit rester dans la sélection (impactés+contrat)
        # au lieu d'être omis pendant que le plan se déclare sûr.
        self.repo.write("src/ameesh/code.py",
                        CODE_V1 + "\n\ndef added(x):\n    return x - 1\n")
        self.repo.commit()
        out = self._classify()
        self.assertEqual(out["unknown_blocks"], ["src/ameesh/code.py::added"])
        self.assertEqual(out["uncovered"], [])
        self.assertTrue(out["phase2_safe"], out["phase2_why"])
        self.assertEqual(out["importers"]["src/ameesh/code.py"],
                         ["test_a", "test_b", "test_c"])
        self.assertTrue(all(out["classes"][m][0] == "P1"
                            for m in ("test_a", "test_b", "test_c")))

    def test_phase2_unsafe_when_no_test_imports_the_changed_file(self):
        # #1339 : sans aucun test importateur (la carte n'a mesuré personne sur
        # ce fichier et le graphe d'import n'en trouve aucun), le doute reste :
        # suite complète, et le fichier est nommé.
        self.repo.write("src/ameesh/code.py", CODE_V1.replace("x * 2", "x * 3"))
        self.repo.commit()
        card = self.repo.card()
        for info in card["blocs"]["src/ameesh/code.py"].values():
            info["modules"] = []
        out = self._classify_with(card, ["test_a", "test_b", "test_c", "test_db"])
        self.assertTrue(out["sure"])
        self.assertEqual(out["uncovered"], ["src/ameesh/code.py"])
        self.assertFalse(out["phase2_safe"])
        self.assertIn("no test is guaranteed to load: src/ameesh/code.py", out["phase2_why"])
        self.assertEqual(out["classes"]["test_a"], ["P3", ""])

    def test_incomplete_map_keeps_the_full_suite(self):
        # #1716 : #1690 nomme un artefact de mesure illisible sans rendre la
        # carte suspecte ; un bloc couvert par l'artefact perdu ressortait donc
        # avec le seul module lisible et la sélection se réduisait en silence.
        # La carte est inutilisable : suite complète.
        self.repo.write("src/ameesh/code.py", CODE_V1.replace("x + 1", "x + 2"))
        self.repo.commit()
        card = self.repo.card()
        card["measure"] = {"parts": 5, "processus": 4, "artefacts_incomplets": [
            {"fichier": "cov/.coverage.coupee",
             "erreur": "DataError: file is not a database"}]}
        card["modules"] = {m: {"durees": {"psycopg": 1.0, "psql": 1.0}, "echecs": []}
                           for m in parts.modules()}
        out = plan.classify(parts.modules(), card, NOW, head="HEAD", repo=self.repo.path)
        self.assertFalse(out["map"]["used"])
        self.assertIn("unreadable measure artefact", out["map"]["problem"])
        self.assertFalse(out["phase2_safe"])
        docs = {"version_only": False, "docs_only": False, "why": "", "files": []}
        with mock.patch.object(plan, "nature", lambda *a, **k: dict(docs)):
            p = plan.make_plan("pull_request", card, NOW, head="HEAD", repo=self.repo.path,
                               base_ref="main", head_ref="lot/1716-carte-incomplete")
        self.assertEqual(p["selection"]["mode"], "full")
        self.assertIn("map not usable", p["selection"]["why"])
        self.assertIn("unreadable measure artefact", p["selection"]["why"])

    def test_new_file_is_covered_by_the_test_that_imports_it(self):
        # #1339 : un fichier nouveau, inconnu de la carte, est couvert par le
        # test du lot qui l'importe (graphe d'import statique) ; la suite
        # complète n'est plus forcée.
        self.repo.write("src/ameesh/new.py", "def thing():\n    return 1\n")
        self.repo.write("tests/test_new.py", "from ameesh.new import thing\n")
        self.repo.commit()
        impact = self.repo.judge(("test_a", "test_b", "test_c", "test_db", "test_new"))
        self.assertIn("src/ameesh/new.py::thing", impact.unknown_blocks)
        self.assertEqual(impact.uncovered, [])
        self.assertEqual(impact.importers, {"src/ameesh/new.py": ["test_new"]})
        self.assertEqual(impact.certified, {"src/ameesh/new.py": ["test_new"]})
        self.assertEqual(impact.p1["test_new"], "src/ameesh/new.py (imported)")

    def test_new_file_no_test_imports_keeps_the_doubt(self):
        # #1339 : un fichier nouveau que rien n'importe (import dynamique
        # compris : aucune arête statique) garde la suite complète.
        self.repo.write("src/ameesh/orphan.py", "def thing():\n    return 1\n")
        self.repo.commit()
        impact = self.repo.judge()
        self.assertIn("src/ameesh/orphan.py::thing", impact.unknown_blocks)
        self.assertEqual(impact.uncovered, ["src/ameesh/orphan.py"])
        self.assertEqual(impact.importers, {})
        self.assertEqual(impact.certified, {})

    def test_new_file_certified_by_type_checking_only_keeps_the_doubt(self):
        # #1339, relecture Codex (B2) : le corps d'un `if TYPE_CHECKING:` ne
        # charge rien à l'exécution ; le seul test qui charge réellement le
        # fichier nouveau passe par importlib (aucune arête statique). Le plan
        # ne peut donc pas certifier la couverture : suite complète, fichier
        # nommé — au lieu de sélectionner le test TYPE_CHECKING et d'omettre
        # le test rouge.
        self.repo.write("tests/test_static.py",
                        "from typing import TYPE_CHECKING\n"
                        "if TYPE_CHECKING:\n    from ameesh.new import VALUE\n")
        self.repo.write("tests/test_dynamic.py",
                        "import importlib\n\nmod = importlib.import_module('ameesh.new')\n")
        self.repo.base = self.repo.commit()
        card = self.repo.card()
        for m in ("test_static", "test_dynamic"):
            card["modules"][m] = {"durees": {"psycopg": 1.0}, "echecs": []}
        self.repo.write("src/ameesh/new.py", "VALUE = 1\n")
        self.repo.commit()
        mods = ["test_a", "test_b", "test_c", "test_db", "test_static", "test_dynamic"]
        impact = plan.judge(card, plan.changed_files(self.repo.base, "HEAD", self.repo.path),
                            mods, "HEAD", self.repo.path)
        self.assertIn("src/ameesh/new.py::<module>", impact.unknown_blocks)
        self.assertEqual(impact.uncovered, ["src/ameesh/new.py"])
        self.assertEqual(impact.importers, {})
        self.assertEqual(impact.certified, {})
        out = self._classify_with(card, mods)
        self.assertFalse(out["phase2_safe"])
        self.assertIn("no test is guaranteed to load: src/ameesh/new.py", out["phase2_why"])

    def test_new_file_with_only_a_lazy_importer_keeps_the_doubt(self):
        # #1339, relecture Codex (B2, résidu) : un import paresseux dans une
        # fonction jamais appelée ne charge rien. Il garde son test dans la
        # sélection (il peut l'appeler), mais ne prouve pas la couverture : le
        # test qui charge réellement le fichier par importlib reste P3, donc
        # la suite complète est conservée au lieu d'omettre le test rouge.
        self.repo.write("tests/test_static.py",
                        "def unused_loader():\n"
                        "    from ameesh import new_feature\n\n"
                        "test_unrelated = True\n")
        self.repo.write("tests/test_dynamic.py",
                        "import importlib\n\n\n"
                        "def test_value():\n"
                        "    mod = importlib.import_module('ameesh.new_feature')\n"
                        "    assert mod.VALUE == -1\n")
        self.repo.base = self.repo.commit()
        card = self.repo.card()
        for m in ("test_static", "test_dynamic"):
            card["modules"][m] = {"durees": {"psycopg": 1.0}, "echecs": []}
        self.repo.write("src/ameesh/new_feature.py", "VALUE = -1\n")
        self.repo.commit()
        mods = ["test_a", "test_b", "test_c", "test_db", "test_static", "test_dynamic"]
        impact = plan.judge(card, plan.changed_files(self.repo.base, "HEAD", self.repo.path),
                            mods, "HEAD", self.repo.path)
        self.assertIn("src/ameesh/new_feature.py::<module>", impact.unknown_blocks)
        self.assertEqual(impact.uncovered, ["src/ameesh/new_feature.py"])
        out = self._classify_with(card, mods)
        self.assertFalse(out["phase2_safe"])
        self.assertIn("no test is guaranteed to load: src/ameesh/new_feature.py", out["phase2_why"])
        # le test paresseux reste sélectionné, le test dynamique reste P3
        self.assertEqual(out["classes"]["test_static"][0], "P1")
        self.assertEqual(out["classes"]["test_dynamic"][0], "P3")
        self.assertEqual(out["importers"], {"src/ameesh/new_feature.py": ["test_static"]})
        self.assertEqual(out["certified"], {})

    def test_new_file_shadowed_by_the_package_init_keeps_the_doubt(self):
        # #1339, relecture Codex (B2 résidu) : `from ameesh import new_feature`
        # ne charge `src/ameesh/new_feature.py` que si le paquet ne lie pas
        # déjà le nom ; un `__init__.py` qui le lie (`new_feature = object()`)
        # laisse l'import du sous-module hors du chargement. Le test statique
        # reste un candidat de sélection, jamais une preuve : le test qui
        # charge réellement le fichier par importlib reste P3, donc la suite
        # complète est conservée au lieu d'omettre le test rouge.
        self.repo.write("tests/test_static.py", "from ameesh import new_feature\n")
        self.repo.write("tests/test_dynamic.py",
                        "import importlib\n\n\n"
                        "def test_value():\n"
                        "    mod = importlib.import_module('ameesh.new_feature')\n"
                        "    assert mod.VALUE == -1\n")
        self.repo.base = self.repo.commit()
        card = self.repo.card()
        for m in ("test_static", "test_dynamic"):
            card["modules"][m] = {"durees": {"psycopg": 1.0}, "echecs": []}
        self.repo.write("src/ameesh/__init__.py", "new_feature = object()\n")
        self.repo.write("src/ameesh/new_feature.py", "VALUE = -1\n")
        self.repo.commit()
        mods = ["test_a", "test_b", "test_c", "test_db", "test_static", "test_dynamic"]
        impact = plan.judge(card, plan.changed_files(self.repo.base, "HEAD", self.repo.path),
                            mods, "HEAD", self.repo.path)
        self.assertIn("src/ameesh/new_feature.py::<module>", impact.unknown_blocks)
        self.assertEqual(impact.uncovered, ["src/ameesh/new_feature.py"])
        # le paquet, lui, est bien chargé par l'import : sa preuve reste
        self.assertEqual(impact.certified, {"src/ameesh/__init__.py": ["test_static"]})
        out = self._classify_with(card, mods)
        self.assertFalse(out["phase2_safe"])
        self.assertIn("no test is guaranteed to load: src/ameesh/new_feature.py", out["phase2_why"])
        # le test statique reste sélectionné, le test dynamique reste P3
        self.assertEqual(out["classes"]["test_static"][0], "P1")
        self.assertEqual(out["classes"]["test_dynamic"][0], "P3")
        self.assertEqual(out["importers"],
                         {"src/ameesh/__init__.py": ["test_static"],
                          "src/ameesh/new_feature.py": ["test_static"]})

    def test_new_file_imported_after_a_raise_in_a_with_keeps_the_doubt(self):
        # #1339, relecture Codex (B2 résidu) : un import placé dans un `with`
        # après un `raise` absorbé (`contextlib.suppress`) n'est jamais
        # atteint ; le corps d'un `with` ne prouve pas un chargement. Le test
        # garde sa place dans la sélection (il peut l'atteindre), le test
        # dynamique reste P3 et la suite complète est conservée.
        self.repo.write("tests/test_static.py",
                        "import contextlib\n"
                        "with contextlib.suppress(RuntimeError):\n"
                        "    raise RuntimeError\n"
                        "    import ameesh.new_feature\n")
        self.repo.write("tests/test_dynamic.py",
                        "import importlib\n\n\n"
                        "def test_value():\n"
                        "    mod = importlib.import_module('ameesh.new_feature')\n"
                        "    assert mod.VALUE == -1\n")
        self.repo.base = self.repo.commit()
        card = self.repo.card()
        for m in ("test_static", "test_dynamic"):
            card["modules"][m] = {"durees": {"psycopg": 1.0}, "echecs": []}
        self.repo.write("src/ameesh/new_feature.py", "VALUE = -1\n")
        self.repo.commit()
        mods = ["test_a", "test_b", "test_c", "test_db", "test_static", "test_dynamic"]
        impact = plan.judge(card, plan.changed_files(self.repo.base, "HEAD", self.repo.path),
                            mods, "HEAD", self.repo.path)
        self.assertIn("src/ameesh/new_feature.py::<module>", impact.unknown_blocks)
        self.assertEqual(impact.uncovered, ["src/ameesh/new_feature.py"])
        self.assertEqual(impact.certified, {})
        out = self._classify_with(card, mods)
        self.assertFalse(out["phase2_safe"])
        self.assertIn("no test is guaranteed to load: src/ameesh/new_feature.py", out["phase2_why"])
        self.assertEqual(out["classes"]["test_static"][0], "P1")
        self.assertEqual(out["classes"]["test_dynamic"][0], "P3")
        self.assertEqual(out["importers"], {"src/ameesh/new_feature.py": ["test_static"]})

    def test_new_file_imported_after_terminal_statement_keeps_the_doubt(self):
        self.repo.write("tests/test_static.py",
                        "raise RuntimeError\nimport ameesh.new_feature\n")
        self.repo.base = self.repo.commit()
        card = self.repo.card()
        card["modules"]["test_static"] = {"durees": {"psycopg": 1.0}, "echecs": []}
        self.repo.write("src/ameesh/new_feature.py", "VALUE = -1\n")
        self.repo.commit()
        mods = ["test_a", "test_b", "test_c", "test_db", "test_static"]
        impact = plan.judge(card, plan.changed_files(self.repo.base, "HEAD", self.repo.path),
                            mods, "HEAD", self.repo.path)
        self.assertEqual(impact.uncovered, ["src/ameesh/new_feature.py"])
        self.assertEqual(impact.certified, {})
        out = self._classify_with(card, mods)
        self.assertFalse(out["phase2_safe"])
        self.assertIn("no test is guaranteed to load: src/ameesh/new_feature.py",
                      out["phase2_why"])
        self.assertEqual(out["classes"]["test_static"][0], "P1")
        self.assertEqual(out["importers"], {"src/ameesh/new_feature.py": ["test_static"]})

    def test_new_file_reached_through_a_changed_importer(self):
        # #1339 : le graphe relie le fichier nouveau au module modifié qui
        # l'importe ; les tests que la carte a mesurés sur ce module (ici un
        # lancement en sous-processus, sans import statique du fichier)
        # couvrent le bloc, sans import direct dans le test.
        self.repo.write("src/ameesh/cli.py", "VALUE = 1\n")
        self.repo.base = self.repo.commit()
        infos, _ = map_mod.blocks("VALUE = 1\n")
        card = self.repo.card()
        card["blocs"]["src/ameesh/cli.py"] = {n: {**i, "modules": ["test_cli"]}
                                              for n, i in infos.items()}
        card["modules"]["test_cli"] = {"durees": {"psycopg": 1.0}, "echecs": []}
        self.repo.write("src/ameesh/new.py", "def thing():\n    return 1\n")
        self.repo.write("src/ameesh/cli.py", "from ameesh.new import thing\n\nVALUE = thing()\n")
        self.repo.commit()
        impact = plan.judge(card, plan.changed_files(self.repo.base, "HEAD", self.repo.path),
                            ["test_a", "test_b", "test_c", "test_db", "test_cli"],
                            "HEAD", self.repo.path)
        self.assertIn("src/ameesh/new.py::thing", impact.unknown_blocks)
        self.assertEqual(impact.uncovered, [])
        self.assertEqual(impact.importers, {"src/ameesh/new.py": ["test_cli"]})

    def test_new_file_plan_stays_impacted(self):
        # #1339, bout en bout : la PR d'un lot qui ajoute un module et son test
        # ne bascule plus en suite complète pour le seul bloc nouveau ; le test
        # nouveau (P0) et le contrat suffisent, le fichier est couvert par son
        # importateur. Noms de modules réels : le découpage lit leurs fichiers.
        real = sorted(parts.modules())
        mods, fresh = real[:5], real[0]
        self.repo.write("src/ameesh/new.py", "def thing():\n    return 1\n")
        self.repo.write(f"tests/{fresh}.py", "from ameesh.new import thing\n")
        self.repo.commit()
        card = self.repo.card()
        card["modules"] = {m: {"durees": {"psycopg": 1.0}, "echecs": []} for m in mods}
        docs = {"version_only": False, "docs_only": False, "why": "", "files": []}
        with mock.patch.object(plan, "nature", lambda *a, **k: dict(docs)), \
                mock.patch.object(plan.parts, "modules", lambda *a: list(mods)), \
                mock.patch.object(plan, "read_list",
                                  lambda path, *a, **k: [mods[1]]
                                  if path == plan.ALWAYS_FILE else []):
            p = plan.make_plan("pull_request", card, NOW, head="HEAD", repo=self.repo.path,
                               base_ref="main", head_ref="lot/x")
        self.assertTrue(p["phase2_safe"], p["phase2_why"])
        self.assertEqual(p["selection"]["mode"], "impacted")
        self.assertEqual(p["classes"][fresh][0], "P0")
        self.assertEqual(p["importers"], {"src/ameesh/new.py": [fresh]})

    def _classify_with(self, card: dict, modules: list[str]) -> dict:
        for m in modules:
            card["modules"].setdefault(m, {"durees": {"psycopg": 1.0}, "echecs": []})
        with mock.patch.object(plan, "ALWAYS_FILE", self.repo.path / "none.txt"):
            return plan.classify(modules, card, NOW, head="HEAD", repo=self.repo.path)

    def test_removed_file_is_a_doubt_and_runs_the_full_suite(self):
        # R2 bureau (B1) : une suppression hors doc ne levait aucun doute
        # (_python et _data traitaient D à part) ; un fichier lu par aucun test
        # mesuré ne lançait que le contrat. Le changement D réel passe par
        # classify/judge et donne la suite complète.
        self.repo.remove("deploy/install.sh")
        self.repo.commit()
        out = self._classify()
        self.assertFalse(out["phase2_safe"])
        self.assertIn("file removed: deploy/install.sh", out["phase2_why"])
        self.assertEqual(out["removed"], ["deploy/install.sh"])
        # make_plan réel : modules réels (le découpage pèse chaque module),
        # carte du dépôt jetable pour le diff ; seul `nature` est simulé.
        mods = parts.modules()
        card = self.repo.card()
        card["modules"] = {m: {"durees": {"psycopg": 1.0}, "echecs": []} for m in mods}
        docs = {"version_only": False, "docs_only": False, "why": "", "files": []}
        with mock.patch.object(plan, "nature", lambda *a, **k: dict(docs)):
            p = plan.make_plan("pull_request", card, NOW, head="HEAD", repo=self.repo.path,
                               base_ref="main", head_ref="lot/x")
        self.assertEqual(p["selection"]["mode"], "full")
        self.assertIn("file removed: deploy/install.sh", p["selection"]["why"])

    def test_documentation_removed_stays_in_the_fast_lane(self):
        # une suppression de documentation ne lève pas ce doute : le doute
        # « fichier supprimé » est réservé à ce que la carte ne peut pas juger
        self.repo.remove("docs/guide.md")
        self.repo.commit()
        out = self._classify()
        self.assertEqual(out["removed"], [])
        self.assertNotIn("file removed", out["unsure"])

    def test_release_pull_request_runs_the_full_suite_with_a_safe_map(self):
        # R1 Codex (B1) : la carte est fraîche et sûre (le bloc modifié est
        # mesuré, phase2_safe), donc une PR de lot ne lancerait que les
        # impactés et le contrat ; une PR de release, par sa source ou sa
        # cible, lance la suite complète, les deux pilotes, sans liste pilote.
        self.repo.write("src/ameesh/code.py", CODE_V1.replace("x + 1", "x + 2"))
        self.repo.commit()
        mods = parts.modules()
        card = self.repo.card()
        card["modules"] = {m: {"durees": {"psycopg": 1.0, "psql": 1.0}, "echecs": []}
                           for m in mods}
        docs = {"version_only": False, "docs_only": False, "why": "", "files": []}
        with mock.patch.object(plan, "nature", lambda *a, **k: dict(docs)):
            for base_ref, head_ref in (("main", "release/1.10.0"),
                                       ("main", "integration/example"),
                                       ("release/1.10.0", "lot/fix")):
                with self.subTest(base=base_ref, head=head_ref):
                    p = plan.make_plan("pull_request", card, NOW, head="HEAD",
                                       repo=self.repo.path,
                                       base_ref=base_ref, head_ref=head_ref)
                    self.assertTrue(p["phase2_safe"], p["phase2_why"])
                    self.assertEqual(p["selection"]["mode"], "full")
                    self.assertIn("release/**", p["selection"]["why"])
                    self.assertEqual([ps["of"] for ps in p["parts"]["psycopg"]],
                                     _full_suite_parts("psycopg"))
                    self.assertEqual([ps["of"] for ps in p["parts"]["psql"]],
                                     _full_suite_parts("psql"))
                    self.assertFalse(any(ps.get("pilot_list") for ps in p["parts"]["psql"]))
                    self.assertEqual(
                        sorted(m for ps in p["parts"]["psycopg"] for m in ps["modules"]), mods)

    def test_version_only_pull_request_on_release_runs_the_full_suite(self):
        # R1 Codex : le même diff (la seule ligne `version` de pyproject.toml)
        # reste sauté sur une PR de lot, mais garde la suite complète sur une
        # PR de release.
        self.repo.write("pyproject.toml", '[project]\nname = "t"\nversion = "1.0.0"\n')
        self.repo.commit()
        self.repo.write("pyproject.toml", '[project]\nname = "t"\nversion = "1.0.1"\n')
        self.repo.commit()
        mods = parts.modules()
        card = self.repo.card()
        card["modules"] = {m: {"durees": {"psycopg": 1.0, "psql": 1.0}, "echecs": []}
                           for m in mods}
        lot = plan.make_plan("pull_request", card, NOW, head="HEAD", repo=self.repo.path,
                             base_ref="main", head_ref="lot/x")
        self.assertTrue(lot["version_only"])
        self.assertEqual(lot["parts"]["psycopg"], [])
        self.assertEqual(plan.outputs(lot)["code"], "false")
        release = plan.make_plan("pull_request", card, NOW, head="HEAD", repo=self.repo.path,
                                 base_ref="main", head_ref="release/1.10.0")
        self.assertFalse(release["version_only"])
        self.assertIn("release/**", release["selection"]["why"])
        self.assertNotEqual(release["parts"]["psycopg"], [])
        self.assertEqual(plan.outputs(release)["code"], "true")
        self.assertEqual([ps["of"] for ps in release["parts"]["psql"]],
                         _full_suite_parts("psql"))

    def test_test_module_read_as_data_selects_its_readers(self):
        # codex (4): the test-module branch skipped the readers of the file
        self.repo.write("tests/test_plan.py", "x = 1\n")
        self.repo.base = self.repo.commit()
        self.repo.write("tests/test_plan.py", "x = 2\n")
        self.repo.commit()
        card = self.repo.card()
        card["donnees"]["tests/test_plan.py"] = ["test_version"]
        out = self._classify_with(card, ["test_a", "test_plan", "test_version"])
        self.assertEqual(out["classes"]["test_plan"], ["P0", "tests/test_plan.py"])
        self.assertEqual(out["classes"]["test_version"], ["P1", "tests/test_plan.py"])

    def test_test_module_added_or_removed_reaches_the_listers_of_tests(self):
        # codex, 3rd review (1): the test-module branch skipped the listers of tests/
        self.repo.write("tests/test_old.py", "x = 1\n")
        self.repo.base = self.repo.commit()
        for why, change in (("added", lambda: self.repo.write("tests/test_plan.py", "x = 1\n")),
                            ("removed", lambda: self.repo.remove("tests/test_old.py"))):
            with self.subTest(why):
                change()
                self.repo.commit()
                card = self.repo.card()
                card["dossiers"]["tests"] = ["test_version"]
                try:
                    out = self._classify_with(card, ["test_a", "test_plan", "test_version"])
                    self.assertEqual(out["classes"]["test_version"][0], "P1", why)
                    self.assertIn(why if why == "removed" else "added",
                                  out["classes"]["test_version"][1])
                finally:
                    _git(self.repo.path, "reset", "-q", "--hard", self.repo.base)

    def test_removed_or_renamed_code_file_reaches_the_directory_listers(self):
        # codex (5): a code file removed (or renamed) changes the listing of its directory
        for why, new_name in (("removed", None), ("renamed", "src/ameesh/code2.py")):
            with self.subTest(why):
                self.repo.remove("src/ameesh/code.py")
                if new_name:
                    self.repo.write(new_name, CODE_V1)
                self.repo.commit()
                card = self.repo.card()
                card["dossiers"]["src/ameesh"] = ["test_lister"]
                try:
                    out = self._classify_with(card, ["test_a", "test_lister"])
                    self.assertEqual(out["classes"]["test_lister"][0], "P1", why)
                finally:
                    _git(self.repo.path, "reset", "-q", "--hard", self.repo.base)

    def test_rewrites_and_bare_drops_are_not_additive(self):
        # L161f review (4)
        for sql in ("create or replace function f() returns int as $$ select 1 $$ language sql;",
                    "create or replace view v as select id from things;",
                    "alter table things drop id;",
                    "alter table things alter column id drop not null;"):
            with self.subTest(sql):
                self.assertTrue(plan._NOT_ADDITIVE.search(sql), sql)
        for sql in ("alter table things add column name text;",
                    "create table if not exists more (id int);",
                    "create index things_id on things (id);"):
            with self.subTest(sql):
                self.assertFalse(plan._NOT_ADDITIVE.search(sql), sql)

    def test_map_problems(self):
        card = self.repo.card()
        self.assertEqual(plan.map_problem(card, NOW, "HEAD", self.repo.path), ("", 0.0))
        self.assertEqual(plan.map_problem(None, NOW)[0], "no map")
        # #3450 : l'âge n'est plus un motif d'inutilisation — la carte rend son
        # âge et l'appelant décide de la fraîcheur exigée.
        old = NOW + datetime.timedelta(hours=49)
        self.assertEqual(plan.map_problem(card, old, "HEAD", self.repo.path), ("", 49.0))
        stranger = dict(card, commit="0" * 40)
        self.assertIn("not an ancestor", plan.map_problem(stranger, NOW, "HEAD",
                                                          self.repo.path)[0])
        # #1716 : une carte bâtie sur des mesures incomplètes (#1690) ne peut
        # plus réduire la sélection à l'insu du plan. L'ancienne graphie
        # `mesure` (L148 stage 6) est lue aussi.
        for cle in ("measure", "mesure"):
            with self.subTest(cle):
                incomplet = dict(card, **{cle: {"artefacts_incomplets": [
                    {"fichier": "cov/.coverage.coupee", "erreur": "DataError"}]}})
                why, _ = plan.map_problem(incomplet, NOW, "HEAD", self.repo.path)
                self.assertIn("unreadable measure artefact", why)
                self.assertIn("cov/.coverage.coupee", why)
        vide = dict(card, measure={"parts": 5, "processus": 5, "artefacts_incomplets": []})
        self.assertEqual(plan.map_problem(vide, NOW, "HEAD", self.repo.path), ("", 0.0))

    def test_map_problem_partial_measure(self):
        """#1257 R1 B3 : une carte dont la fusion a écarté des couvertures est
        partielle ; elle ne juge plus la sélection, même fraîche et ancêtre."""
        card = self.repo.card()  # carte ancienne : la clé n'existe pas
        self.assertEqual(plan.ignored_coverages(card), [])
        self.assertEqual(plan.map_problem(card, NOW, "HEAD", self.repo.path)[0], "")
        vide = dict(card, measure={"couverture_ignoree": []})  # carte neuve sans perte
        self.assertEqual(plan.map_problem(vide, NOW, "HEAD", self.repo.path)[0], "")
        partielle = dict(card, measure={"couverture_ignoree": [
            {"part": "mesure-psycopg-1", "fichier": ".coverage.tronquee",
             "raison": "tronqué (12 pages annoncées, 11 écrites)"}]})
        probleme, age = plan.map_problem(partielle, NOW, "HEAD", self.repo.path)
        self.assertIn("partial measure", probleme)
        self.assertIsNone(age)  # le refus ne dépend ni de la date ni du dépôt
        # la forme ancienne (clé française) est lue aussi
        ancienne = dict(card, mesure={"couverture_ignoree": [{"part": "m"}]})
        self.assertIn("partial measure",
                      plan.map_problem(ancienne, NOW, "HEAD", self.repo.path)[0])

    def test_always_and_recent_failures(self):
        changes = [("M", "src/ameesh/work.py"), ("A", "src/ameesh/migrations/0099_x.sql")]
        self.assertEqual(plan.always_hits(changes, ["test_cluster"]),
                         {"test_cluster": "always list (src/ameesh/migrations/0099_x.sql)"})
        self.assertEqual(plan.always_hits([("M", "src/ameesh/work.py")], ["test_cluster"]), {})
        # #749 : le runner declenche la liste, comme une migration ou le SQL
        self.assertEqual(plan.always_hits([("M", "src/ameesh/runner.py")], ["test_l31_orphans"]),
                         {"test_l31_orphans": "always list (src/ameesh/runner.py)"})
        card = {"modules": {
            "test_recent": {"echecs": [{"ts": "2026-10-09T00:00:00+00:00"}]},
            "test_old": {"echecs": [{"ts": "2026-09-30T00:00:00+00:00"}]}}}
        self.assertEqual(list(plan.recent_failures(card, NOW)), ["test_recent"])

    def test_always_list_sorted_and_known(self):
        always = plan.read_list(plan.ALWAYS_FILE, parts.modules())
        self.assertEqual(always, ["test_2340_auditeur_role",
                                  "test_2449_garde_mecanique",
                                  "test_2457_garde_non_python",
                                  "test_339_orphan_settle",
                                  "test_3752_gabarit",
                                  "test_4256_validation_deadlines",
                                  "test_4259_client_literals",
                                  "test_4299_agent_rename",
                                  "test_cluster", "test_events_attach", "test_l31_orphans",
                                  "test_l50_group_orphan_work_items",
                                  "test_l72_panne_de_base", "test_migrations"])

    def test_psql_pr_modules_adds_the_triggered_always_list(self):
        # #749 : la liste « always » declenchee rejoint le pilote psql d'une PR ;
        # sans declenchement, la selection reste exactement tests/parts/pilote-psql.txt
        self.assertEqual(plan.psql_pr_modules(["test_a", "test_cluster"],
                                              ["test_l31_orphans"]),
                         ["test_a", "test_cluster", "test_l31_orphans"])
        self.assertEqual(plan.psql_pr_modules(["test_a"], []), ["test_a"])

    def test_new_functions_taken_out_losslessly(self):
        # taking nothing out must give the very fingerprints of the map
        for f in sorted((REPO / "src").rglob("*.py")):
            source = f.read_text(encoding="utf-8")
            def prints(blocks):
                return {k: v["empreinte"] for k, v in blocks.items()}
            self.assertEqual(prints(plan._without_new_functions(source, set())),
                             prints(map_mod.blocks(source)[0]), f)


class ImportGraphTest(unittest.TestCase):
    """#1339 : le graphe d'import statique qui couvre un fichier inconnu de la
    carte. Résolution des noms, imports relatifs et paresseux compris ; le
    graphe est lu dans git (tête jugée, pas l'arbre de travail)."""

    def setUp(self):
        self.repo = _Repo()
        self.addCleanup(self.repo.close)

    def test_dotted_names(self):
        self.assertEqual(plan._dotted("src/ameesh/db.py"), "ameesh.db")
        self.assertEqual(plan._dotted("src/ameesh/storage/__init__.py"), "ameesh.storage")
        self.assertEqual(plan._dotted("tests/test_a.py"), "tests.test_a")
        self.assertEqual(plan._dotted("scripts/tool.py"), "")
        self.assertEqual(plan._dotted("docs/guide.md"), "")

    def test_resolution_absolute_relative_and_tests(self):
        known = {"src/ameesh/db.py", "src/ameesh/__init__.py",
                 "src/ameesh/storage/__init__.py", "src/ameesh/storage/work.py",
                 "tests/support.py", "tests/__init__.py"}
        cases = {
            "src/ameesh/x.py": ("import ameesh.db\n", "src/ameesh/db.py"),
            "src/ameesh/y.py": ("from ameesh import db\n", "src/ameesh/db.py"),
            "src/ameesh/storage/z.py": ("from . import work\n", "src/ameesh/storage/work.py"),
            "src/ameesh/storage/z2.py": ("from .work import connect\n",
                                         "src/ameesh/storage/work.py"),
            "src/ameesh/storage/z3.py": ("from .. import db\n", "src/ameesh/db.py"),
            "src/ameesh/storage/__init__.py": ("from . import work\n",
                                               "src/ameesh/storage/work.py"),
            "tests/test_a.py": ("from tests.support import PgTestCase\n", "tests/support.py"),
            "tests/test_b.py": ("def f():\n    import ameesh.db\n", "src/ameesh/db.py"),
            "tests/test_c.py": ("from ameesh.db import *\n", "src/ameesh/db.py"),
        }
        for path, (source, target) in cases.items():
            with self.subTest(path):
                self.assertIn(target, plan._imported_files(source, path, known))
        for source in ("import os\nimport json\n", "def broken(:\n"):
            with self.subTest(source=source):
                self.assertEqual(plan._imported_files(source, "tests/test_d.py", known), set())

    def test_type_checking_import_is_not_a_runtime_edge(self):
        # #1339, relecture Codex (B2) : le corps d'un `if TYPE_CHECKING:` ne
        # charge rien à l'exécution ; il ne peut pas certifier qu'un test
        # charge un fichier nouveau. Les formes usuelles sont couvertes,
        # l'import réel du même fichier reste lu.
        known = {"src/ameesh/db.py"}
        dead = {
            "nom importé": "from typing import TYPE_CHECKING\n"
                           "if TYPE_CHECKING:\n    from ameesh import db\n",
            "typing pointé": "import typing\n"
                             "if typing.TYPE_CHECKING:\n    from ameesh import db\n",
            "alias": "from typing import TYPE_CHECKING as TC\n"
                     "if TC:\n    from ameesh import db\n",
            "constante fausse": "if False:\n    from ameesh import db\n",
        }
        for why, source in dead.items():
            with self.subTest(why):
                self.assertEqual(plan._imported_files(source, "tests/test_a.py", known),
                                 set())
        live = ("from typing import TYPE_CHECKING\n"
                "if TYPE_CHECKING:\n    import ameesh.missing\n"
                "import ameesh.db\n")
        self.assertEqual(plan._imported_files(live, "tests/test_a.py", known),
                         {"src/ameesh/db.py"})
        # `not TYPE_CHECKING` est vrai à l'exécution : l'import reste une arête
        negated = ("from typing import TYPE_CHECKING\n"
                   "if not TYPE_CHECKING:\n    import ameesh.db\n")
        self.assertEqual(plan._imported_files(negated, "tests/test_a.py", known),
                         {"src/ameesh/db.py"})

    def test_guaranteed_edges_only_the_module_body_itself(self):
        # #1339, relecture Codex (B2 résiduel) : seul un import que le corps du
        # module exécute à l'import prouve le chargement. Un import paresseux,
        # conditionnel, sous `try` ou dans une boucle reste une arête possible
        # (il peut tourner : sélection), jamais une preuve.
        known = {"src/ameesh/db.py"}
        guarantees = {
            "direct": "import ameesh.db\n",
            "corps de classe": "class C:\n    import ameesh.db\n",
            "else d'un garde mort": "if False:\n    import ameesh.missing\n"
                                    "else:\n    import ameesh.db\n",
        }
        for why, source in guarantees.items():
            with self.subTest(why):
                self.assertEqual(plan._imported_files(source, "tests/test_a.py", known,
                                                      guaranteed=True),
                                 {"src/ameesh/db.py"}, why)
        candidates = {
            "paresseux": "def loader():\n    import ameesh.db\n",
            "conditionnel": "import sys\nif sys.version_info:\n    import ameesh.db\n",
            "try": "try:\n    import ameesh.db\nexcept ImportError:\n    pass\n",
            "boucle": "for _ in ():\n    import ameesh.db\n",
            "with": "import contextlib\n"
                    "with contextlib.suppress(RuntimeError):\n    import ameesh.db\n",
            "TYPE_CHECKING": "from typing import TYPE_CHECKING\n"
                             "if TYPE_CHECKING:\n    import ameesh.db\n",
        }
        for why, source in candidates.items():
            with self.subTest(why):
                self.assertEqual(plan._imported_files(source, "tests/test_a.py", known,
                                                      guaranteed=True), set(), why)
                if why != "TYPE_CHECKING":  # mort : ni preuve ni candidat
                    self.assertEqual(plan._imported_files(source, "tests/test_a.py", known),
                                     {"src/ameesh/db.py"}, why)

    def test_terminal_statement_prevents_later_import_proof(self):
        known = {"src/ameesh/db.py"}
        for terminal in ("raise RuntimeError", "import sys\nsys.exit(1)"):
            with self.subTest(terminal=terminal):
                source = terminal + "\nimport ameesh.db\n"
                self.assertEqual(plan._imported_files(source, "tests/test_a.py", known,
                                                      guaranteed=True), set())
                # Keep the possible edge for selection, even without proof.
                self.assertEqual(plan._imported_files(source, "tests/test_a.py", known),
                                 known)
        # Class bodies execute during the containing module's import too.
        source = "class C:\n    raise RuntimeError\nimport ameesh.db\n"
        self.assertEqual(plan._imported_files(source, "tests/test_a.py", known,
                                              guaranteed=True), set())

    def test_terminal_statement_keeps_earlier_and_nonexecuted_proofs(self):
        known = {"src/ameesh/db.py"}
        for source in (
            "import ameesh.db\nraise RuntimeError\n",
            "import ameesh.db\nimport sys\nsys.exit(1)\n",
            "def unused():\n    raise RuntimeError\nimport ameesh.db\n",
            "def unused():\n    import sys\n    sys.exit(1)\nimport ameesh.db\n",
            "if False:\n    raise RuntimeError\nimport ameesh.db\n",
        ):
            with self.subTest(source=source):
                self.assertEqual(plan._imported_files(source, "tests/test_a.py", known,
                                                      guaranteed=True), known)
    def test_guaranteed_edges_stop_at_a_module_level_termination(self):
        # #1627 : une terminaison atteignable à l'import (`raise` gardé, garde
        # d'exception, exit, assert faux) laisse les imports suivants possibles,
        # jamais garantis — sinon le plan certifie un chargement qui peut ne pas
        # avoir eu lieu. Sens conservateur : une preuve en moins, jamais une
        # fausse preuve.
        known = {"src/ameesh/db.py"}
        stopped = {
            "raise gardé": "import os\n"
                           "if not os.environ.get('PG'):\n"
                           "    raise RuntimeError\n"
                           "import ameesh.db\n",
            "raise dans un except": "try:\n"
                                    "    import psycopg\n"
                                    "except ImportError:\n"
                                    "    raise SystemExit\n"
                                    "import ameesh.db\n",
            "exit": "exit()\nimport ameesh.db\n",
            "quit": "quit()\nimport ameesh.db\n",
            "os._exit": "import os\nos._exit(1)\nimport ameesh.db\n",
            "os._exit aliasé": "from os import _exit as bye\nbye(1)\nimport ameesh.db\n",
            "sys.exit": "import sys\nsys.exit(1)\nimport ameesh.db\n",
            "sys.exit aliasé": "import sys as s\ns.exit(1)\nimport ameesh.db\n",
            "exit importé": "from sys import exit as bye\nbye(1)\nimport ameesh.db\n",
            "assert faux": "assert False\nimport ameesh.db\n",
            "raise avant, import dans le même corps de classe":
                "class C:\n    raise RuntimeError\n    import ameesh.db\n",
        }
        for why, source in stopped.items():
            with self.subTest(why):
                self.assertEqual(plan._imported_files(source, "tests/test_a.py", known,
                                                      guaranteed=True), set(), why)
                self.assertEqual(plan._imported_files(source, "tests/test_a.py", known),
                                 {"src/ameesh/db.py"}, why)
        # un garde mort ne termine rien : `if TYPE_CHECKING:` ne s'exécute pas
        dead = ("from typing import TYPE_CHECKING\n"
                "if TYPE_CHECKING:\n    raise RuntimeError\n"
                "import ameesh.db\n")
        self.assertEqual(plan._imported_files(dead, "tests/test_a.py", known,
                                              guaranteed=True), {"src/ameesh/db.py"})
        # le corps d'une fonction ne tourne pas à l'import : son raise ne coupe
        # pas la preuve du corps du module
        deferred = ("def broken():\n    raise RuntimeError\n"
                    "import ameesh.db\n")
        self.assertEqual(plan._imported_files(deferred, "tests/test_a.py", known,
                                              guaranteed=True), {"src/ameesh/db.py"})
        nested = ("def factory():\n"
                  "    class C:\n        raise RuntimeError\n"
                  "import ameesh.db\n")
        self.assertEqual(plan._imported_files(nested, "tests/test_a.py", known,
                                              guaranteed=True), {"src/ameesh/db.py"})

    def test_exit_expressions_stop_proofs_before_class_body_and_after_assert(self):
        # #1474 : les bases et décorateurs s'évaluent avant le corps de classe ;
        # une sortie dans un assert s'exécute aussi, même sans assert faux.
        known = {"src/ameesh/db.py"}
        sources = (
            "import sys\nclass C(sys.exit(1)):\n    import ameesh.db\n",
            "@exit()\nclass C:\n    import ameesh.db\n",
            "import os as o\nclass C(metaclass=o._exit(1)):\n    import ameesh.db\n",
            "from sys import exit as bye\nassert bye(1)\nimport ameesh.db\n",
            "import os\nassert os._exit(1)\nimport ameesh.db\n",
        )
        for source in sources:
            with self.subTest(source=source):
                self.assertEqual(plan._imported_files(source, "tests/test_a.py", known,
                                                      guaranteed=True), set())
                self.assertEqual(plan._imported_files(source, "tests/test_a.py", known),
                                 known)

    def test_exit_expressions_stop_proofs_in_function_definitions(self):
        # #1474 : décorateurs, valeurs par défaut et annotations d'une fonction
        # ou d'un lambda s'évaluent à la définition ; seul le corps est différé.
        known = {"src/ameesh/db.py"}
        sources = (
            "@exit()\ndef f():\n    pass\nimport ameesh.db\n",
            "@exit()\nasync def f():\n    pass\nimport ameesh.db\n",
            "import sys\ndef f(x=sys.exit(1)):\n    pass\nimport ameesh.db\n",
            "import os as o\ndef f(*, x=o._exit(1)):\n    pass\nimport ameesh.db\n",
            "def f(x: quit()):\n    pass\nimport ameesh.db\n",
            "g = lambda a=quit(): 0\nimport ameesh.db\n",
        )
        for source in sources:
            with self.subTest(source=source):
                self.assertEqual(plan._imported_files(source, "tests/test_a.py", known,
                                                      guaranteed=True), set())
                self.assertEqual(plan._imported_files(source, "tests/test_a.py", known),
                                 known)
        for source in ("def f():\n    exit()\nimport ameesh.db\n",
                       "f = lambda: exit()\nimport ameesh.db\n",
                       "@staticmethod\ndef f(x=1):\n    import os\nimport ameesh.db\n"):
            with self.subTest(source=source):
                self.assertEqual(plan._imported_files(source, "tests/test_a.py", known,
                                                      guaranteed=True), known)

    def test_exit_expressions_keep_prior_dead_and_deferred_proofs(self):
        known = {"src/ameesh/db.py"}
        sources = (
            "import ameesh.db\nclass C(exit()):\n    pass\n",
            "if False:\n    class C(exit()):\n        pass\nimport ameesh.db\n",
            "def factory():\n    class C(exit()):\n        pass\nimport ameesh.db\n",
            "def unused():\n    assert quit()\nimport ameesh.db\n",
            "class C:\n    import ameesh.db\n    quit()\n",
        )
        for source in sources:
            with self.subTest(source=source):
                self.assertEqual(plan._imported_files(source, "tests/test_a.py", known,
                                                      guaranteed=True), known)

    def test_reassigned_exit_symbols_stay_conservative(self):
        # #1474 : un symbole de terminaison réaffecté (`sys = ...`, puis un
        # `bye = ...` sur un nom importé de sys/os) n'est pas suivi — la
        # réaffectation peut être conditionnelle, donc la preuve reste coupée :
        # une preuve perdue, jamais une preuve fausse. L'arête de sélection
        # possible est conservée.
        known = {"src/ameesh/db.py"}
        reassigned = {
            "sys rebindé": "import sys\n"
                           "sys = None\n"
                           "sys.exit(1)\n"
                           "import ameesh.db\n",
            "sys rebindé sans sortie": "import sys\n"
                                       "class Fake:\n"
                                       "    @staticmethod\n"
                                       "    def exit(code):\n"
                                       "        return None\n"
                                       "sys = Fake\n"
                                       "sys.exit(0)\n"
                                       "import ameesh.db\n",
            "exit importé puis rebindé": "from sys import exit as bye\n"
                                         "bye = print\n"
                                         "bye('x')\n"
                                         "import ameesh.db\n",
            "exit builtin rebindé": "exit = print\n"
                                    "exit('x')\n"
                                    "import ameesh.db\n",
            "os._exit aliasé puis rebindé": "from os import _exit as bye\n"
                                            "bye = print\n"
                                            "bye('x')\n"
                                            "import ameesh.db\n",
        }
        for why, source in reassigned.items():
            with self.subTest(why):
                self.assertEqual(plan._imported_files(source, "tests/test_a.py", known,
                                                      guaranteed=True), set(), why)
                self.assertEqual(plan._imported_files(source, "tests/test_a.py", known),
                                 {"src/ameesh/db.py"}, why)

    def test_assigned_exit_aliases_and_star_imports_stop_proofs(self):
        # #1860 : un alias de sortie par affectation (`e = sys.exit ; e()`) et
        # `_exit()` après `from os import *` terminent aussi l'import : les
        # imports suivants restent des arêtes possibles, jamais des preuves.
        known = {"src/ameesh/db.py"}
        stopped = {
            "sys.exit affecté": "import sys\n"
                                "e = sys.exit\n"
                                "e(1)\n"
                                "import ameesh.db\n",
            "os._exit affecté": "import os as o\n"
                                "e = o._exit\n"
                                "e(1)\n"
                                "import ameesh.db\n",
            "exit importé affecté": "from sys import exit as bye\n"
                                    "e = bye\n"
                                    "e(1)\n"
                                    "import ameesh.db\n",
            "quit builtin affecté": "q = quit\n"
                                    "q()\n"
                                    "import ameesh.db\n",
            "chaîne d'alias": "import sys\n"
                              "e = sys.exit\n"
                              "f = e\n"
                              "f(1)\n"
                              "import ameesh.db\n",
            "alias appelé dans son affectation": "import sys\n"
                                                  "(e := sys.exit)(1)\n"
                                                  "import ameesh.db\n",
            "alias importé appelé dans son affectation": "from sys import exit as bye\n"
                                                           "(e := bye)(1)\n"
                                                           "import ameesh.db\n",
            "affectation multiple": "import sys\n"
                                    "e = f = sys.exit\n"
                                    "f(1)\n"
                                    "import ameesh.db\n",
            "cible multiple lisible": "import sys\n"
                                      "e, f = sys.exit, print\n"
                                      "e(1)\n"
                                      "import ameesh.db\n",
            "affectation gardée": "import sys\n"
                                  "if callable(sys):\n"
                                  "    e = sys.exit\n"
                                  "e(1)\n"
                                  "import ameesh.db\n",
            "affectation différée": "import sys\n"
                                    "def quit_now():\n"
                                    "    global e\n"
                                    "    e = sys.exit\n"
                                    "quit_now()\n"
                                    "e(1)\n"
                                    "import ameesh.db\n",
            "from os import *": "from os import *\n"
                                "_exit(1)\n"
                                "import ameesh.db\n",
        }
        for why, source in stopped.items():
            with self.subTest(why):
                self.assertEqual(plan._imported_files(source, "tests/test_a.py", known,
                                                      guaranteed=True), set(), why)
                self.assertEqual(plan._imported_files(source, "tests/test_a.py", known),
                                 {"src/ameesh/db.py"}, why)
        # une affectation qui ne porte pas de sortie ne coupe aucune preuve
        kept = {
            "appel ordinaire": "e = print\n"
                               "e('x')\n"
                               "import ameesh.db\n",
            "lambda ordinaire": "e = lambda: 0\n"
                                "e()\n"
                                "import ameesh.db\n",
            "cible multiple ordinaire": "e, f = 1, 2\n"
                                        "e()\n"
                                        "import ameesh.db\n",
            "nom affecté jamais appelé": "import sys\n"
                                         "e = sys.exit\n"
                                         "import ameesh.db\n",
        }
        for why, source in kept.items():
            with self.subTest(why):
                self.assertEqual(plan._imported_files(source, "tests/test_a.py", known,
                                                      guaranteed=True), known, why)

    def test_assigned_module_aliases_and_getattr_exits_stop_proofs(self):
        # #1878, N de la relecture Claude de #1860 : un module sys/os renommé
        # par affectation (`s = sys` puis `s.exit()`) et une lecture statique
        # `getattr(sys, 'exit')` sont des sorties comme les autres : les
        # imports suivants restent des arêtes possibles, jamais des preuves.
        known = {"src/ameesh/db.py"}
        stopped = {
            "sys renommé": "import sys\n"
                           "s = sys\n"
                           "s.exit(1)\n"
                           "import ameesh.db\n",
            "os renommé": "import os\n"
                          "o = os\n"
                          "o._exit(1)\n"
                          "import ameesh.db\n",
            "module importé aliasé puis renommé": "import sys as s0\n"
                                                  "s = s0\n"
                                                  "s.exit(1)\n"
                                                  "import ameesh.db\n",
            "chaîne de renommages": "import sys\n"
                                    "s = sys\n"
                                    "t = s\n"
                                    "t.exit(1)\n"
                                    "import ameesh.db\n",
            "paire de modules": "import sys, os\n"
                                "s, o = sys, os\n"
                                "o._exit(1)\n"
                                "import ameesh.db\n",
            "getattr sys.exit": "import sys\n"
                                "getattr(sys, 'exit')(1)\n"
                                "import ameesh.db\n",
            "getattr os._exit": "import os\n"
                                "getattr(os, '_exit')(1)\n"
                                "import ameesh.db\n",
            "getattr avec défaut": "import sys\n"
                                   "getattr(sys, 'exit', None)(1)\n"
                                   "import ameesh.db\n",
            "getattr via module renommé": "import sys\n"
                                          "s = sys\n"
                                          "getattr(s, 'exit')(1)\n"
                                          "import ameesh.db\n",
            "getattr affecté puis appelé": "import sys\n"
                                           "e = getattr(sys, 'exit')\n"
                                           "e(1)\n"
                                           "import ameesh.db\n",
            # #1906 : le porteur peut être affecté par walrus — la preuve
            # d'import doit tomber comme pour un nom simple.
            "walrus sys.exit": "import sys\n"
                               "(s := sys).exit(1)\n"
                               "import ameesh.db\n",
            "getattr walrus sys.exit": "import sys\n"
                                       "getattr((s := sys), 'exit')(1)\n"
                                       "import ameesh.db\n",
            # #2098 : walrus imbriqué — le déballage doit boucler sur les
            # deux sites ; profondeur bornée (quatre niveaux) pour la boucle.
            "walrus imbriqué sys.exit": "import sys\n"
                                        "((a := (b := sys)).exit(1))\n"
                                        "import ameesh.db\n",
            "getattr walrus imbriqué": "import sys\n"
                                       "getattr((a := (b := sys)), 'exit')(1)\n"
                                       "import ameesh.db\n",
            "walrus imbriqué à quatre niveaux": "import sys\n"
                                                "((a := (b := (c := (d := sys)))).exit(1))\n"
                                                "import ameesh.db\n",
        }
        for why, source in stopped.items():
            with self.subTest(why):
                self.assertEqual(plan._imported_files(source, "tests/test_a.py", known,
                                                      guaranteed=True), set(), why)
                self.assertEqual(plan._imported_files(source, "tests/test_a.py", known),
                                 {"src/ameesh/db.py"}, why)
        # une simple mention ne coupe rien : ni un module renommé non utilisé,
        # ni un getattr d'un autre membre, d'un autre porteur ou dynamique
        kept = {
            "module renommé jamais appelé": "import sys\n"
                                            "s = sys\n"
                                            "import ameesh.db\n",
            "getattr d'un autre membre": "import sys\n"
                                         "getattr(sys, 'version')\n"
                                         "import ameesh.db\n",
            "getattr d'un porteur ordinaire": "class C:\n"
                                              "    pass\n"
                                              "getattr(C, 'exit')\n"
                                              "import ameesh.db\n",
            "getattr au nom dynamique": "import sys\n"
                                        "name = 'exit'\n"
                                        "getattr(sys, name)\n"
                                        "import ameesh.db\n",
            # #1906 : déballer un walrus ne classe pas un porteur ordinaire.
            "getattr walrus d'un porteur ordinaire": "class C:\n"
                                                     "    pass\n"
                                                     "getattr((x := C), 'exit')\n"
                                                     "import ameesh.db\n",
            # #2098 : idem quand le walrus du porteur ordinaire est imbriqué.
            "getattr walrus imbriqué d'un porteur ordinaire": "class C:\n"
                                                              "    pass\n"
                                                              "getattr((a := (b := C)), 'exit')\n"
                                                              "import ameesh.db\n",
        }
        for why, source in kept.items():
            with self.subTest(why):
                self.assertEqual(plan._imported_files(source, "tests/test_a.py", known,
                                                      guaranteed=True), known, why)

    def test_guaranteed_package_import_does_not_prove_the_member(self):
        # #1339, relecture Codex (B2 résidu) : `from ameesh import db` ne
        # prouve pas que `ameesh/db.py` est chargé — le paquet peut lier le nom
        # dans son `__init__` (`db = object()`) et l'import du sous-module n'a
        # alors pas lieu. Le module résolu par l'import (le paquet) reste une
        # preuve, l'alias `module.membre` n'est qu'un candidat de sélection ;
        # les formes explicites (`import ameesh.db`, `from ameesh.db import x`)
        # gardent la preuve.
        known = {"src/ameesh/__init__.py", "src/ameesh/db.py"}
        source = "from ameesh import db\n"
        self.assertEqual(plan._imported_files(source, "tests/test_a.py", known,
                                              guaranteed=True),
                         {"src/ameesh/__init__.py"})
        self.assertEqual(plan._imported_files(source, "tests/test_a.py", known),
                         {"src/ameesh/__init__.py", "src/ameesh/db.py"})
        for why, explicit in {
            "module importé": "import ameesh.db\n",
            "module importé aliasé": "import ameesh.db as db\n",
            "membre du module": "from ameesh.db import connect\n",
        }.items():
            with self.subTest(why):
                self.assertEqual(plan._imported_files(explicit, "tests/test_a.py", known,
                                                      guaranteed=True),
                                 {"src/ameesh/__init__.py", "src/ameesh/db.py"}, why)

    def test_import_graph_reaches_the_test_through_the_changed_module(self):
        self.repo.write("src/ameesh/new.py", "def thing():\n    return 1\n")
        self.repo.write("src/ameesh/cli.py", "from ameesh.new import thing\n")
        self.repo.write("tests/test_cli.py", "import ameesh.cli\n")
        self.repo.write("tests/test_other.py", "import json\n")
        self.repo.commit()
        reverse = plan._import_graph("HEAD", self.repo.path)
        self.assertEqual(plan._reverse_closure(reverse, "src/ameesh/new.py"),
                         {"src/ameesh/new.py", "src/ameesh/cli.py", "tests/test_cli.py"})
        self.assertEqual(plan._reverse_closure(reverse, "tests/test_other.py"),
                         {"tests/test_other.py"})


class DocsLaneTest(unittest.TestCase):
    """make_plan end to end on a throwaway repository (codex review of L161f)."""

    ROLES = ["test_a", "test_b", "test_c", "test_db", "test_lister"]

    def setUp(self):
        self.repo = _Repo()
        self.addCleanup(self.repo.close)
        # real module names: the L161b heuristic reads their text
        real = [m for m in parts.modules() if m != "test_l161f_plan"][:len(self.ROLES)]
        self.name = dict(zip(self.ROLES, real))
        self.MODULES = sorted(real)
        card = self.repo.card()

        def rename(v):
            if isinstance(v, dict):
                return {k: rename(x) for k, x in v.items()}
            if isinstance(v, list):
                return [self.name.get(x, x) if isinstance(x, str) else rename(x) for x in v]
            return v
        self.card = {k: rename(v) if k in ("blocs", "donnees", "dossiers", "tables") else v
                     for k, v in card.items()}
        self.card["modules"] = {m: {"durees": {"psycopg": 1.0}, "echecs": []}
                                for m in self.MODULES}

    def _read_list(self, path, known, required=False):
        # un contrat non vide : sans lui, un repli de la voie documentation
        # rendrait la suite complète par « contrat vide » et masquerait une
        # sélection réduite (#3450).
        if path == plan.ALWAYS_FILE:
            return [self.MODULES[0]]
        if path == plan.PILOT_FILE:
            return list(self.MODULES)
        return []

    def _plan(self, files: list[str], event: str = "pull_request") -> dict:
        docs = {"version_only": False, "docs_only": event == "pull_request",
                "why": "docs", "files": files}
        with mock.patch.object(plan, "nature", lambda *a, **k: dict(docs)), \
                mock.patch.object(plan.parts, "modules", lambda *a: list(self.MODULES)), \
                mock.patch.object(plan, "read_list", self._read_list):
            return plan.make_plan(event, self.card, NOW, head="HEAD", repo=self.repo.path,
                                  base_ref="main", head_ref="lot/x")

    def _base_with_unmeasured_ci_change(self):
        self.card["genere"] = (NOW - datetime.timedelta(hours=6.8)).isoformat()
        self.repo.write(".github/workflows/tests.yml", "name: tests\n")
        self.repo.write("scripts/test-plan.py", "# CI changed on main after the map\n")
        return self.repo.commit()

    def _real_plan(self, base):
        with mock.patch.object(plan.parts, "modules", lambda *a: list(self.MODULES)), \
                mock.patch.object(plan, "read_list", self._read_list):
            return plan.make_plan("pull_request", self.card, NOW, base=base, head="HEAD",
                                  repo=self.repo.path, base_ref="main", head_ref="lot/x")

    def test_docs_pr_ignores_ci_changes_already_in_its_base(self):
        base = self._base_with_unmeasured_ci_change()
        self.card["donnees"]["docs/guide.md"] = [self.name["test_c"]]
        self.repo.write("docs/guide.md", "# Updated guide\n")
        self.repo.commit()

        p = self._real_plan(base)
        self.assertEqual(p["files"], ["docs/guide.md"])
        self.assertTrue(p["map"]["used"])
        self.assertTrue(p["docs_only"], p["why"])
        self.assertIn(self.name["test_c"], p["doc_readers"])
        self.assertEqual(p["parts"], {"psycopg": [], "psql": []})
        self.assertEqual(plan.outputs(p)["code"], "false")

    def test_docs_pr_with_old_base_map_keeps_database_readers(self):
        base = self._base_with_unmeasured_ci_change()
        self.card["donnees"]["docs/guide.md"] = [self.name["test_db"]]
        self.repo.write("docs/guide.md", "# Updated guide\n")
        self.repo.commit()

        p = self._real_plan(base)
        self.assertFullSuite(p, self.name["test_db"])
        self.assertEqual(p["classes"][self.name["test_db"]][0], "P1")

    def test_docs_pr_keeps_a_database_reader_changed_on_its_base(self):
        self.card["blocs"]["src/ameesh/code.py"]["used"]["modules"] = [self.name["test_db"]]
        self._base_with_unmeasured_ci_change()
        self.repo.write("src/ameesh/code.py", CODE_V1.replace(
            "return x + 1", "return open('docs/guide.md').read()"))
        base = self.repo.commit()
        self.repo.write("docs/guide.md", "# Updated guide\n")
        self.repo.commit()

        # The old map did not see this function read the guide. Its recorded
        # test still has to run when the base changes that function.
        p = self._real_plan(base)
        self.assertFullSuite(p, self.name["test_db"])

    def test_docs_pr_keeps_unmeasured_base_code_in_the_full_suite(self):
        self._base_with_unmeasured_ci_change()
        self.repo.write("src/ameesh/code.py", CODE_V1 +
                        "\n\ndef new_reader():\n    return open('docs/guide.md').read()\n")
        base = self.repo.commit()
        self.repo.write("docs/guide.md", "# Updated guide\n")
        self.repo.commit()

        self.assertFullSuite(self._real_plan(base), "documentation base contains unmeasured code")

    def test_mixed_pr_with_old_base_map_does_not_take_docs_lane(self):
        base = self._base_with_unmeasured_ci_change()
        self.repo.write("src/ameesh/code.py", CODE_V1.replace("return x + 1", "return x + 2"))
        self.repo.write("docs/guide.md", "# Updated guide\n")
        self.repo.commit()

        p = self._real_plan(base)
        self.assertEqual(sorted(p["files"]), ["docs/guide.md", "src/ameesh/code.py"])
        self.assertFalse(p["docs_only"], p["why"])
        self.assertEqual(plan.outputs(p)["code"], "true")
        # #3450 : le contrat du banc n'est plus vide (il masquait la sélection
        # d'un lot derrière « contrat vide ») ; la PR mixte suit bien le lot —
        # les impactés (`used` → test_a) et le contrat —, pas la suite complète.
        self.assertEqual(p["selection"]["mode"], "impacted")
        self.assertEqual(sorted(m for part in p["parts"]["psycopg"]
                                for m in part["modules"]), [self.name["test_a"]])

    def test_missing_uncertainty_diagnostic_keeps_the_full_suite(self):
        with mock.patch.object(plan, "read_list", return_value=[]):
            state = plan.classify(self.MODULES, self.card, NOW, repo=self.repo.path)
        for metadata, why in ((None, "map unusable"), (state["map"], "selection unsure")):
            with self.subTest(why=why), mock.patch.object(plan, "classify", return_value={
                    **state, "map": metadata, "sure": False, "phase2_safe": False,
                    "unsure": []}):
                self.assertFullSuite(self._plan(["docs/guide.md"]), why)

    def assertFullSuite(self, p: dict, why: str) -> None:
        self.assertFalse(p["docs_only"], p["why"])
        self.assertIn(why, p["why"])
        self.assertEqual(p["doc_readers"], [])
        self.assertEqual(sorted(m for ps in p["parts"]["psycopg"] for m in ps["modules"]),
                         self.MODULES)
        self.assertEqual(plan.outputs(p)["code"], "true")

    def test_vanished_directory_reaches_the_listers_above_it(self):
        # codex, 3rd review (2): docs/sub, known to the map, vanishes with its only
        # file; the listing of docs/ changes too
        self.repo.write("docs/sub/only.md", "# Only\n")
        self.repo.base = self.card["commit"] = self.repo.commit()
        self.card["dossiers"]["docs"] = [self.name["test_lister"]]
        self.card["dossiers"]["docs/sub"] = [self.name["test_c"]]
        self.repo.remove("docs/sub/only.md")
        self.repo.commit()
        p = self._plan(["docs/sub/only.md"])
        self.assertTrue(p["docs_only"], p["why"])
        self.assertIn(self.name["test_lister"], p["doc_readers"])
        self.assertIn(self.name["test_c"], p["doc_readers"])

    def test_read_by_a_plain_test_stays_in_the_fast_lane(self):
        self.card["donnees"]["docs/guide.md"] = [self.name["test_c"]]
        self.repo.write("docs/guide.md", "# Guide, again\n")
        self.repo.commit()
        p = self._plan(["docs/guide.md"])
        self.assertTrue(p["docs_only"])
        self.assertIn(self.name["test_c"], p["doc_readers"])
        self.assertEqual(p["parts"]["psycopg"], [])

    def test_read_by_a_database_test_runs_the_full_suite(self):
        self.card["donnees"]["docs/guide.md"] = [self.name["test_c"], self.name["test_db"]]
        self.repo.write("docs/guide.md", "# Guide, again\n")
        self.repo.commit()
        p = self._plan(["docs/guide.md"])
        self.assertFullSuite(p, self.name["test_db"])
        self.assertEqual(p["classes"][self.name["test_db"]][0], "P1")

    def test_stale_map_keeps_the_documentation_lane_on_the_full_suite(self):
        # #3450 : une carte périmée juge encore le diff d'un lot, mais la voie
        # documentation saute les parts : elle exige une carte fraîche, sinon
        # les lecteurs ajoutés depuis la carte manqueraient.
        self.card["donnees"]["docs/guide.md"] = [self.name["test_db"]]
        self.card["genere"] = (NOW - datetime.timedelta(hours=49)).isoformat()
        self.repo.write("docs/guide.md", "# Guide, again\n")
        self.repo.commit()
        self.assertFullSuite(self._plan(["docs/guide.md"]), "map not usable")
        self.assertFullSuite(self._plan_without_map(["docs/guide.md"]), "no map")

    def test_partial_measure_card_runs_the_full_suite(self):
        """#1257 R1 B3 : une couverture écartée à la fusion rend la carte
        partielle ; le jugement de sélection est refusé et le repli suite
        complète s'applique, même carte fraîche et ancêtre."""
        self.card["measure"] = {"couverture_ignoree": [
            {"part": "mesure-psycopg-1", "fichier": ".coverage.tronquee",
             "raison": "tronqué (12 pages annoncées, 11 écrites)"}]}
        self.card["donnees"]["docs/guide.md"] = [self.name["test_db"]]
        self.repo.write("docs/guide.md", "# Guide, again\n")
        self.repo.commit()
        p = self._plan(["docs/guide.md"])
        self.assertFullSuite(p, "partial measure")
        self.assertFalse(p["map"]["used"])
        self.assertFalse(p["sure"])

    def _plan_without_map(self, files):
        self.card = None
        return self._plan(files)

    def test_document_added_or_removed_reaches_the_directory_listers(self):
        # codex (2): only `donnees` was read; a lister of docs/ touching a table ran nowhere
        self.card["dossiers"]["docs"] = [self.name["test_db"]]
        for change in ("add", "remove"):
            with self.subTest(change):
                if change == "add":
                    self.repo.write("docs/new.md", "# New\n")
                    files = ["docs/new.md"]
                else:
                    self.repo.remove("docs/guide.md")
                    files = ["docs/guide.md"]
                self.repo.commit()
                p = self._plan(files)
                self.assertFullSuite(p, self.name["test_db"])
                self.assertEqual(p["classes"][self.name["test_db"]][0], "P1")

    def test_python_file_read_as_data_selects_its_readers(self):
        # codex (3): a .py read as data, changed without an AST change, left its reader in P3
        self.card["donnees"]["src/ameesh/code.py"] = [self.name["test_db"]]
        self.repo.write("src/ameesh/code.py", CODE_V1 + "# a comment, same AST\n")
        self.repo.commit()
        p = self._plan([], event="push")
        self.assertEqual(p["classes"][self.name["test_db"]], ["P1", "src/ameesh/code.py"])


def _coverage_disponible() -> bool:
    try:
        import coverage  # noqa: F401
    except ImportError:
        return False
    return True


class PartialMeasureTest(unittest.TestCase):
    """#1257 R1 B3 : de la collecte (`scripts/map.py`) à la décision de
    sélection (`scripts/test-plan.py`)."""

    @unittest.skipUnless(_coverage_disponible(), "coverage requis")
    def test_collection_to_plan_refuses_a_partial_card(self):
        """Une part dont un fichier de couverture est illisible donne une carte
        partielle ; le plan refuse alors de juger la sélection (repli suite
        complète) au lieu de croire la mesure complète."""
        dossier = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, dossier)
        part = dossier / "mesure-psycopg-1"
        (part / "cov").mkdir(parents=True)
        (part / "cov" / ".coverage.tronquee").write_bytes(b"pas du sqlite")
        # carte fraîche et ancêtre de HEAD : seule la perte la rend inutilisable
        card = map_mod.merge([str(part)], _git(REPO, "rev-parse", "HEAD"))
        self.assertEqual(len(card["mesure"]["couverture_ignoree"]), 1)
        # #1690 : le meme artefact tronque est aussi nomme dans
        # `artefacts_incomplets` ; le temoin du temoin doit retirer les DEUX
        # projections de la perte, sinon il n'est plus sain.
        mesure = dict(card["mesure"], couverture_ignoree=[],
                      artefacts_incomplets=[])
        saine = dict(card, measure=mesure, mesure=mesure)  # témoin du témoin
        self.assertEqual(plan.map_problem(saine, NOW, "HEAD", REPO)[0], "")
        probleme, _ = plan.map_problem(card, NOW, "HEAD", REPO)
        self.assertIn("partial measure", probleme)
        out = plan.classify(parts.modules(), card, NOW, head="HEAD", repo=REPO)
        self.assertFalse(out["map"]["used"])
        self.assertFalse(out["sure"])
        self.assertFalse(out["phase2_safe"])
        self.assertIn("partial measure", out["unsure"][0])

    @unittest.skipUnless(_coverage_disponible(), "coverage requis")
    def test_collection_to_plan_refuses_a_zero_byte_coverage(self):
        """R1 B3 #1257 : un fichier de couverture de 0 octet est une perte
        conservatrice ; la carte est partielle et le plan refuse de juger la
        sélection (repli suite complète). Le contrôle sain (même part sans le
        fichier vide) reste utilisable."""
        from coverage import CoverageData
        dossier = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, dossier)
        part = dossier / "mesure-psycopg-1"
        (part / "cov").mkdir(parents=True)
        vide = part / "cov" / ".coverage.vide"
        vide.write_bytes(b"")
        valide = CoverageData(basename=str(part / "cov" / ".coverage.valide"))
        valide.set_context("tests.test_l1.T.test_a")
        valide.add_lines({str(REPO / "src" / "ameesh" / "db.py"): {1}})
        valide.write()
        card = map_mod.merge([str(part)], _git(REPO, "rev-parse", "HEAD"))
        ignorees = card["mesure"]["couverture_ignoree"]
        self.assertEqual(len(ignorees), 1)
        self.assertIn("vide (0 octet)", ignorees[0]["raison"])
        probleme, _ = plan.map_problem(card, NOW, "HEAD", REPO)
        self.assertIn("partial measure", probleme)
        out = plan.classify(parts.modules(), card, NOW, head="HEAD", repo=REPO)
        self.assertFalse(out["map"]["used"])
        self.assertFalse(out["sure"])
        self.assertFalse(out["phase2_safe"])
        self.assertIn("partial measure", out["unsure"][0])
        # témoin du témoin : sans le fichier vide, la même mesure est utilisable
        vide.unlink()
        saine = map_mod.merge([str(part)], _git(REPO, "rev-parse", "HEAD"))
        self.assertEqual(saine["mesure"]["couverture_ignoree"], [])
        self.assertEqual(plan.map_problem(saine, NOW, "HEAD", REPO)[0], "")
class InheritedFoundationTest(unittest.TestCase):
    """#1314 : le socle CI que la cible a déjà changé depuis la carte ne force
    plus la suite complète d'une branche de lot ; le changement du socle par
    la branche elle-même la force toujours, et le repli reste conservateur
    quand le range propre de la branche est illisible (règle du bureau,
    14/10/2026 : une carte en retard ne déclenche jamais le repli pour un
    changement déjà dans la cible)."""

    def setUp(self):
        self.repo = _Repo()
        self.addCleanup(self.repo.close)

    def _target_moves_after_the_map(self) -> str:
        """La cible bouge après la carte : deux fichiers du socle."""
        self.repo.write(".github/workflows/tests.yml", "name: tests\n")
        self.repo.write("scripts/test-parts.py", "# CI de la cible\n")
        return self.repo.commit()

    def _plan(self, base: str = "HEAD^1", card: dict | None = None) -> dict:
        mods = parts.modules()
        card = card or self.repo.card()
        card["modules"] = {m: {"durees": {"psycopg": 1.0}, "echecs": []} for m in mods}
        nature = {"version_only": False, "docs_only": False, "why": "", "files": []}
        with mock.patch.object(plan, "nature", lambda *a, **k: dict(nature)):
            return plan.make_plan("pull_request", card, NOW, base=base, head="HEAD",
                                  repo=self.repo.path, base_ref="main", head_ref="lot/x")

    def _ran(self, p: dict) -> list[str]:
        return sorted(m for ps in p["parts"]["psycopg"] for m in ps["modules"])

    def test_socle_herite_ne_force_pas_la_suite_complete(self):
        target = self._target_moves_after_the_map()
        self.repo.write("src/ameesh/code.py", CODE_V1.replace("x + 1", "x + 2"))
        self.repo.commit()
        card = self.repo.card()
        touched = parts.modules()[0]
        card["blocs"]["src/ameesh/code.py"]["used"]["modules"] = [touched]
        p = self._plan(base=target, card=card)
        self.assertTrue(p["map"]["used"])
        self.assertEqual(p["foundation"], [])
        self.assertTrue(p["sure"], p["unsure"])
        self.assertTrue(p["phase2_safe"], p["phase2_why"])
        self.assertEqual(p["selection"]["mode"], "impacted")
        self.assertIn(touched, self._ran(p))

    def test_le_lot_qui_change_le_socle_force_la_suite_complete(self):
        target = self._target_moves_after_the_map()
        self.repo.write("tests/support.py", "# helper\n")
        self.repo.commit()
        p = self._plan(base=target)
        self.assertFalse(p["phase2_safe"])
        self.assertEqual(p["foundation"], ["tests/support.py"])
        self.assertEqual(p["selection"]["mode"], "full")
        self.assertIn("test infrastructure changed: tests/support.py",
                      p["selection"]["why"])

    def test_range_propre_illisible_reste_conservateur(self):
        self._target_moves_after_the_map()
        self.repo.write("src/ameesh/code.py", CODE_V1.replace("x + 1", "x + 2"))
        self.repo.commit()
        p = self._plan(base="range-illisible")
        self.assertFalse(p["phase2_safe"])
        self.assertEqual(p["selection"]["mode"], "full")
        self.assertIn("test infrastructure changed", p["selection"]["why"])

    def test_head_hors_fusion_refuse_le_range_propre(self):
        # HEAD^1 n'est la cible que sur le commit de fusion d'une PR ; sur un
        # commit simple c'est le précédent de la branche : le range propre
        # serait incomplet, le doute du socle hérité est conservé.
        self._target_moves_after_the_map()
        self.repo.write("src/ameesh/code.py", CODE_V1.replace("x + 1", "x + 2"))
        self.repo.commit()
        p = self._plan()
        self.assertFalse(p["phase2_safe"])
        self.assertEqual(p["selection"]["mode"], "full")
        self.assertIn("test infrastructure changed", p["selection"]["why"])

    def test_version_seule_du_lot_reste_muette_malgre_la_cible(self):
        # L157 : l'exemption se juge sur le range propre de la branche ; un
        # pyproject.toml bougé par la cible après la carte ne la casse plus.
        self.repo.write("pyproject.toml", '[project]\nname = "t"\nversion = "1.0.0"\n')
        map_commit = self.repo.commit()
        self.repo.write("pyproject.toml",
                        '[project]\nname = "t"\nversion = "1.0.0"\ndependencies = []\n')
        target = self.repo.commit()  # la cible change pyproject.toml après la carte
        self.repo.write("pyproject.toml",
                        '[project]\nname = "t"\nversion = "1.0.1"\ndependencies = []\n')
        self.repo.commit()  # le lot ne change que la ligne `version`
        card = self.repo.card()
        card["commit"] = map_commit
        p = self._plan(base=target, card=card)
        self.assertEqual(p["foundation"], [])
        self.assertTrue(p["sure"], p["unsure"])
        self.assertTrue(p["phase2_safe"], p["phase2_why"])


class InheritedDataTest(unittest.TestCase):
    """#1476 : une donnée non Python que la cible a changée depuis la carte et
    qu'aucun test mesuré ne lit ne force plus la suite complète de la branche ;
    changée par la branche elle-même, elle reste un doute (repli sûr)."""

    def setUp(self):
        self.repo = _Repo()
        self.addCleanup(self.repo.close)

    def _plan(self, base: str = "HEAD^1", card: dict | None = None) -> dict:
        mods = parts.modules()
        card = card or self.repo.card()
        card["modules"] = {m: {"durees": {"psycopg": 1.0}, "echecs": []} for m in mods}
        nature = {"version_only": False, "docs_only": False, "why": "", "files": []}
        with mock.patch.object(plan, "nature", lambda *a, **k: dict(nature)):
            return plan.make_plan("pull_request", card, NOW, base=base, head="HEAD",
                                  repo=self.repo.path, base_ref="main", head_ref="lot/x")

    def _ran(self, p: dict) -> list[str]:
        return sorted(m for ps in p["parts"]["psycopg"] for m in ps["modules"])

    def test_donnee_heritee_sans_lecteur_mesure_ne_force_pas_la_suite(self):
        # la cible change `deploy/install.sh`, absent de `donnees` (aucun
        # lecteur mesuré) ; le lot, lui, change un fichier mesuré.
        self.repo.write("deploy/install.sh", "#!/bin/sh\necho cible\n")
        target = self.repo.commit()
        self.repo.write("src/ameesh/code.py", CODE_V1.replace("x + 1", "x + 2"))
        self.repo.commit()
        card = self.repo.card()
        touched = parts.modules()[0]
        card["blocs"]["src/ameesh/code.py"]["used"]["modules"] = [touched]
        p = self._plan(base=target, card=card)
        self.assertTrue(p["map"]["used"])
        self.assertTrue(p["sure"], p["unsure"])
        self.assertTrue(p["phase2_safe"], p["phase2_why"])
        self.assertEqual(p["selection"]["mode"], "impacted")
        self.assertIn(touched, self._ran(p))

    def test_donnee_du_lot_sans_lecteur_mesure_force_la_suite(self):
        # le lot change lui-même une donnée que la carte ne sait pas lire :
        # aucun test ne peut être sélectionné pour elle, le repli reste.
        self.repo.write("src/ameesh/code.py", CODE_V1.replace("x + 1", "x + 2"))
        self.repo.commit()
        self.repo.write("deploy/install.sh", "#!/bin/sh\necho lot\n")
        self.repo.commit()
        p = self._plan(base="HEAD^1", card=self.repo.card())
        self.assertFalse(p["phase2_safe"])
        self.assertEqual(p["selection"]["mode"], "full")
        self.assertIn("read by no measured test", p["selection"]["why"])

    def test_range_propre_illisible_reste_conservateur_sur_une_donnee(self):
        # range propre illisible : la donnée héritée est jugée comme changée
        # par la branche, donc le doute tient.
        self.repo.write("deploy/install.sh", "#!/bin/sh\necho cible\n")
        self.repo.commit()
        self.repo.write("src/ameesh/code.py", CODE_V1.replace("x + 1", "x + 2"))
        self.repo.commit()
        p = self._plan(base="range-illisible")
        self.assertFalse(p["phase2_safe"])
        self.assertEqual(p["selection"]["mode"], "full")
        self.assertIn("read by no measured test", p["selection"]["why"])


class PushLaneTest(unittest.TestCase):
    """nature() on a push: the whole pushed range decides (#833)."""

    def setUp(self):
        self.repo = _Repo()
        self.addCleanup(self.repo.close)

    def _nature(self, base, head="HEAD", ref="refs/heads/main", event="push"):
        return plan.nature(event, base=base, head=head, repo=self.repo.path, ref=ref)

    def _doc_commit(self, name, text="# Doc\n"):
        self.repo.write(f"docs/{name}", text)
        return self.repo.commit()

    def test_multi_commit_documentation_push_takes_the_fast_lane(self):
        # #833: github.event.before, not HEAD^1, so every pushed commit counts
        self._doc_commit("a.md")
        head = self._doc_commit("b.md")
        out = self._nature(self.repo.base, head)
        self.assertTrue(out["docs_only"], out["why"])
        self.assertEqual(sorted(out["files"]), ["docs/a.md", "docs/b.md"])

    def test_code_hidden_behind_a_last_documentation_commit_runs_the_suite(self):
        # the bug: HEAD^1 alone saw only the last commit, documentation, and
        # took the fast lane over a push that carried code
        self.repo.write("src/ameesh/code.py", CODE_V1.replace("x + 1", "x + 2"))
        code = self.repo.commit()
        head = self._doc_commit("note.md")
        last_only = self._nature(code, head)
        self.assertTrue(last_only["docs_only"], last_only["why"])
        out = self._nature(self.repo.base, head)
        self.assertFalse(out["docs_only"], out["why"])
        self.assertIn("src/ameesh/code.py", out["files"])

    def test_a_push_to_a_release_branch_always_runs_the_suite(self):
        head = self._doc_commit("a.md")
        out = self._nature(self.repo.base, head, ref="refs/heads/release/1.10.0")
        self.assertFalse(out["docs_only"], out["why"])
        self.assertIn("full suite", out["why"])
        self.assertIn("release/1.10.0", out["why"])

    def test_created_branch_and_rewritten_history_run_the_suite(self):
        head = self._doc_commit("a.md")
        for base in (plan.NULL_SHA, "", "not a sha"):
            with self.subTest(base or "empty"):
                out = self._nature(base, head)
                self.assertFalse(out["docs_only"], out["why"])
                self.assertIn("pushed range unknown", out["why"])
        # a rewritten history: the tip before the push is no ancestor anymore
        _git(self.repo.path, "checkout", "-q", "-b", "side", self.repo.base)
        side = self._doc_commit("side.md")
        _git(self.repo.path, "checkout", "-q", "-")
        out = self._nature(side, head)
        self.assertFalse(out["docs_only"], out["why"])
        self.assertIn("pushed range unknown", out["why"])

    def test_a_pushed_version_bump_runs_the_suite(self):
        # L157 is a pull-request rule: a pushed version never takes the lane
        self.repo.write("pyproject.toml", '[project]\nversion = "1.2.3"\n')
        base = self.repo.commit()
        self.repo.write("pyproject.toml", '[project]\nversion = "1.2.4"\n')
        head = self.repo.commit()
        out = self._nature(base, head)
        self.assertFalse(out["version_only"], out["why"])
        self.assertFalse(out["docs_only"], out["why"])

    def test_a_pull_request_is_unchanged(self):
        # the reference of the source branch does not matter on a PR
        head = self._doc_commit("a.md")
        out = self._nature(self.repo.base, head, ref="refs/heads/lot/833-x",
                           event="pull_request")
        self.assertTrue(out["docs_only"], out["why"])


class PlanTest(unittest.TestCase):
    def test_fallback_without_a_map(self):
        p = plan.make_plan("push", None, NOW)
        self.assertFalse(p["sure"])
        self.assertFalse(p["phase2_safe"])
        self.assertEqual(p["unsure"], ["no map"])
        mods = parts.modules()
        self.assertEqual(sorted(m for ps in p["parts"]["psycopg"] for m in ps["modules"]), mods)
        # #3749 : hors nocturne, psql passe sa liste pilote et le contrat
        pilot = set(parts.read_list(plan.PILOT_FILE, mods)) | set(
            plan.read_list(plan.ALWAYS_FILE, mods))
        self.assertEqual(sorted(m for ps in p["parts"]["psql"] for m in ps["modules"]),
                         sorted(pilot))
        # #1476 : la suite complète passe à 16 psql et 8 psycopg
        self.assertEqual([ps["of"] for ps in p["parts"]["psycopg"]],
                         _full_suite_parts("psycopg"))
        self.assertEqual([ps["of"] for ps in p["parts"]["psql"]],
                         _full_suite_parts("psql"))
        out = plan.outputs(p)
        self.assertEqual(json.loads(out["psycopg"]),
                         list(range(1, plan.PARTS["psycopg"][1] + 1)))
        self.assertEqual(out["psql_parts"], str(plan.PARTS["psql"][1]))
        self.assertEqual(out["code"], "true")

    def test_pull_request_runs_the_pilot_modules_with_psql(self):
        p = plan.make_plan("pull_request", None, NOW, base_ref="main", head_ref="lot/x")
        if p["version_only"] or p["docs_only"]:
            self.skipTest("the checked out commit is a version or documentation change")
        pilot = parts.read_list(plan.PILOT_FILE, parts.modules())
        psql = p["parts"]["psql"]
        # #947 puis #3343 : pas de carte -> repli suite complète, donc les
        # parts de main (16), les modules pilotes une fois chacun.
        self.assertEqual([ps["part"] for ps in psql],
                         list(range(1, plan.PARTS["psql"][1] + 1)))
        self.assertEqual([ps["of"] for ps in psql], _full_suite_parts("psql"))
        seen = [m for ps in psql for m in ps["modules"]]
        self.assertEqual(len(seen), len(set(seen)), "un module dans deux parts")
        self.assertTrue(set(pilot) <= set(seen), sorted(set(pilot) - set(seen)))

    def test_pull_request_psql_part_is_split_for_the_cap(self):
        # #947 : huit jobs se partagent chaque hôte auto-hébergé ; une part
        # pilote unique pesait 21,7-26,6 min au repos (médiane 24,5) pour un
        # plafond de 30 min et l'atteignait dès que la flotte était saturée
        # (24/24 runners). Trois parts pèsent ~8 min chacune, ce qui tient au
        # ralentissement mesuré de 2,5-3x : ne pas redescendre à une part.
        self.assertGreaterEqual(plan.PARTS["psql"][0], 3)
        # #1476 : seize parts psql et huit psycopg (24 jobs = 24 runners),
        # d'après les durées mesurées du run de paquet 39085758208.
        self.assertEqual(plan.PARTS["psql"][1], 16)
        self.assertEqual(plan.PARTS["psycopg"][1], 8)

    def test_full_suite_psycopg_is_split_for_the_cap(self):
        # #2292 : à quatre parts, 14,3-30,5 min sous saturation sur les cinq
        # derniers paquets ; les parts 4/4 de h-1800 et 1/4 de h-1900 ont été
        # coupées au plafond de 30 min sans test en échec. La suite complète
        # passe à six parts (~15-20 min visées) et le plafond du job à 45 min ;
        # le pilote d'une PR de lot reste à quatre parts (n_pr).
        self.assertGreaterEqual(plan.PARTS["psycopg"][1], 6)
        self.assertEqual(plan.PARTS["psycopg"][0], 4)

    def test_pull_request_psql_runs_the_always_modules_when_triggered(self):
        # #749 : un diff qui touche migrations/, db.py, le SQL ou le runner
        # declenche la liste « always » (contrat de base, baux et orphelins) ;
        # ces modules passent alors AUSSI sous psql des la PR, au lieu d'attendre
        # la suite complete de main ou la CI de release.
        mods = parts.modules()
        always = ["test_339_orphan_settle", "test_cluster", "test_l31_orphans"]
        card = {"schema": map_mod.SCHEMA, "commit": "0" * 40, "genere": NOW.isoformat(),
                "modules": {m: {"durees": {"psycopg": 1.0, "psql": 1.0}, "echecs": []}
                            for m in mods},
                "blocs": {}, "donnees": {}, "dossiers": {}, "tables": {}, "migrations": {}}
        classes = {m: (["P1", "always list (src/ameesh/runner.py)"]
                       if m in always else ["P3", ""]) for m in mods}
        with mock.patch.object(plan, "nature", return_value={
                "version_only": False, "docs_only": False, "why": "code",
                "files": ["src/ameesh/runner.py"]}), \
                mock.patch.object(plan, "classify", return_value={
                "map": None, "sure": False, "phase2_safe": False, "unsure": [],
                "untested": [], "unknown_blocks": [], "foundation": [], "changed": 1,
                "always": always,
                "classes": {m: list(c) for m, c in classes.items()}}):
            p = plan.make_plan("pull_request", card, NOW, base_ref="main", head_ref="lot/x")
        if p["version_only"] or p["docs_only"]:
            self.skipTest("the checked out commit is a version or documentation change")
        pilot = parts.read_list(plan.PILOT_FILE, parts.modules())
        seen = sorted(m for ps in p["parts"]["psql"] for m in ps["modules"])
        self.assertEqual(seen, sorted(set(pilot) | set(always)))
        # sans declenchement, la selection reste exactement le pilote
        self.assertEqual(plan.psql_pr_modules(pilot, []), pilot)

    def test_parts_impacted_first(self):
        classes = {"test_canon": ("P3", ""), "test_cluster": ("P1", "x"),
                   "test_version": ("P0", "y"), "test_cost": ("P2", "z")}
        card = {"modules": {m: {"durees": {"psycopg": 1.0}} for m in classes}}
        split = plan.split(list(classes), "psycopg", 1, card, classes)
        self.assertEqual(split[0]["modules"],
                         ["test_version", "test_cluster", "test_cost", "test_canon"])

    def test_skipped_parts_and_annotations(self):
        p = {"version_only": True, "docs_only": False, "doc_readers": [],
             "parts": {"psycopg": [], "psql": []},
             "map": {"commit": "abcdef1234567890"},
             "untested": [{"file": "src/ameesh/x.py", "block": "f", "lines": [3, 9]}]}
        out = plan.outputs(p)
        self.assertEqual((out["psycopg"], out["psql"], out["code"]), ("[]", "[]", "false"))
        self.assertEqual(plan.annotations(p), [
            "::notice file=src/ameesh/x.py,line=3,endLine=9,title=Changed lines without a "
            "test::f changed and no test executes it (map abcdef123456)"])

    def test_stale_map_is_announced_in_annotations_and_summary(self):
        # #3343 : une carte de plus de 48 h ne reste plus silencieuse — le
        # plan porte un avertissement et le résumé le dit.
        p = plan.make_plan("push", None, NOW)
        p["map"] = {"commit": "a" * 40, "generated": "2026-10-17T16:02:49+00:00",
                    "age_h": 163.0, "used": False,
                    "problem": "map older than 48 h (163 h)"}
        lines = plan.annotations(p)
        self.assertTrue(lines and lines[0].startswith("::warning"), lines)
        self.assertIn("163 h", lines[0])
        self.assertIn("carte périmée", plan.summary(p))
        fresh = dict(p, map={"commit": "b" * 40, "generated": NOW.isoformat(),
                             "age_h": 2.0, "used": True, "problem": ""})
        self.assertEqual(plan.annotations(fresh), [])
        self.assertNotIn("carte périmée", plan.summary(fresh))

    def test_fallback_plan_lets_the_parts_split(self):
        # refs explicites : ne pas lire le GITHUB_* ambiant, où une tête de
        # paquet integration/** est une PR de release (canon §3)
        p = plan.fallback_plan("pull_request", "boom", base_ref="main", head_ref="lot/x")
        f = pathlib.Path(tempfile.mkdtemp()) / "plan.json"
        self.addCleanup(shutil.rmtree, f.parent)
        f.write_text(json.dumps(p), encoding="utf-8")
        # psycopg : sans liste, test-parts.py découpe la suite lui-même ;
        # psql (#3749, R1 cx3) : la liste pilote et le contrat, explicites
        self.assertIsNone(parts.read_plan(f, "psycopg", 1))
        self.assertIsNotNone(parts.read_plan(f, "psql", 1))
        self.assertTrue(parts._driver_plan(str(f)))
        self.assertEqual(json.loads(plan.outputs(p)["psycopg"]),
                         list(range(1, plan.PARTS["psycopg"][1] + 1)))
        # #3343 : le repli d'une branche de lot est la suite complète, il prend
        # donc le découpage de main (16 parts psql), pas les trois d'un lot.
        self.assertEqual(json.loads(plan.outputs(p)["psql"]),
                         list(range(1, plan.PARTS["psql"][1] + 1)))
        self.assertEqual([ps["of"] for ps in p["parts"]["psql"]],
                         _full_suite_parts("psql"))

    def test_each_module_in_exactly_one_part_with_the_map(self):
        mods = parts.modules()
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, check=True,
                              capture_output=True, text=True).stdout.strip()
        card = {"schema": map_mod.SCHEMA, "commit": head, "genere": NOW.isoformat(),
                "modules": {m: {"durees": {"psycopg": 1.0 + 7 * (i % 5), "psql": 2.0 + i % 3},
                                "echecs": []} for i, m in enumerate(mods)},
                "blocs": {}, "donnees": {}, "dossiers": {}, "tables": {}, "migrations": {}}
        p = plan.make_plan("push", card, NOW)
        self.assertTrue(p["map"]["used"], p["map"])
        # #749 : une fusion a main passe la suite complete, pas seulement la
        # CI de release. #3749 : psycopg en entier, psql sur sa liste pilote et
        # tout le contrat, dans le découpage de la suite complète ; le
        # nocturne (`psql_full`) passe toute la suite sous psql.
        self.assertEqual([ps["of"] for ps in p["parts"]["psql"]],
                         _full_suite_parts("psql"))
        pilot = set(parts.read_list(plan.PILOT_FILE, mods)) | set(
            plan.read_list(plan.ALWAYS_FILE, mods))
        night = plan.make_plan("schedule", card, NOW, psql_full=True)
        cases = {"psycopg": (p, mods), "psql": (p, sorted(pilot)),
                 "psql nocturne": (night, mods)}
        for name, (got, expected) in cases.items():
            with self.subTest(driver=name):
                seen = [m for ps in got["parts"][name.split()[0]] for m in ps["modules"]]
                self.assertEqual(sorted(seen), expected)  # every one, none twice
        self.assertEqual(night["parts"]["psycopg"], [])

    def _main_plan(self) -> dict:
        d = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d)
        old, sys.stdout = sys.stdout, io.StringIO()
        try:
            code = plan.main(["plan", "--out", str(d / "plan.json"), "--event", "push"])
        finally:
            sys.stdout = old
        self.assertEqual(code, 0)
        return json.loads((d / "plan.json").read_text(encoding="utf-8"))

    def test_exception_in_make_plan_falls_back_to_the_full_suite(self):
        def boom(*a, **k):
            raise KeyError("blocs")
        with mock.patch.object(plan, "make_plan", boom):
            p = self._main_plan()
        self.assertEqual(p["why"], "planning failed")
        self.assertIn("KeyError", p["unsure"][0])
        self.assertTrue(all(ps["modules"] is None for ps in p["parts"]["psycopg"]))

    def test_stale_always_entry_falls_back_instead_of_failing(self):
        # L161f review (3): lire_liste raised SystemExit, out of reach of `except Exception`
        d = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d)
        (d / "toujours.txt").write_text("test_gone_long_ago\n", encoding="utf-8")
        with mock.patch.object(plan, "ALWAYS_FILE", d / "toujours.txt"):
            p = self._main_plan()
        self.assertEqual(p["why"], "planning failed")
        self.assertIn("test_gone_long_ago", p["unsure"][0])

    def test_command_line_never_fails_on_a_bad_map(self):
        d = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d)
        (d / "carte.json.gz").write_bytes(b"not gzip")
        out = io.StringIO()
        old, sys.stdout = sys.stdout, out
        try:
            code = plan.main(["plan", "--out", str(d / "plan.json"), "--map",
                              str(d / "carte.json.gz"), "--event", "push",
                              "--github-output", str(d / "out")])
        finally:
            sys.stdout = old
        self.assertEqual(code, 0)
        self.assertIn(f"psycopg={list(range(1, plan.PARTS['psycopg'][1] + 1))}",
                      (d / "out").read_text(encoding="utf-8"))


def _full_suite_parts(driver: str) -> list[int]:
    """`of` de chaque part de la suite complète, lu de `plan.PARTS`.

    Le découpage appartient au plan (#1692 : 9 parts psql ; #1476 : 16 psql et
    8 psycopg), épinglé par
    `PlanTest.test_pull_request_psql_part_is_split_for_the_cap`. Un témoin de
    cette voie le lit au lieu de recopier le nombre : sinon chaque changement
    de découpage casse l'union des lots (#1343 × #1476, 5 FAIL sur l'arbre
    composé du paquet 1700).
    """
    n = plan.PARTS[driver][1]
    return [n] * n


class LotBranchTest(unittest.TestCase):
    """L1211 (canon §3) : une branche de lot ne lance que les tests impactés,
    plus toujours les tests de contrat, et se replie sur la suite complète
    quand la sélection ne peut pas décider.

    Témoins du sélecteur et de la porte, sans base : le plan est bâti sur un
    état `classify` qui vaut carte fraîche et sûre sur la vraie liste des
    modules, et le workflow porte les déclencheurs et le check requis.
    """

    def setUp(self):
        self.mods = parts.modules()
        self.contract = plan.read_list(plan.ALWAYS_FILE, self.mods)
        self.pilot = parts.read_list(plan.PILOT_FILE, self.mods)

    def _classes(self, **impacted):
        classes = {m: ("P3", "") for m in self.mods}
        for m, why in impacted.items():
            classes[m] = ("P1", why)
        return classes

    def _state(self, classes, **kw):
        state = {
            "map": {"commit": "c" * 40, "generated": NOW.isoformat(), "age_h": 0.0,
                    "used": True, "problem": "", "stale": False},
            "sure": True, "phase2_safe": True, "lot_safe": True,
            "phase2_why": "", "unsure": [],
            "untested": [], "unknown_blocks": [], "foundation": [], "changed": 2,
            "always": [], "contract": list(self.contract),
            "classes": {m: list(c) for m, c in classes.items()},
        }
        state.update(kw)
        return state

    def _plan(self, event="pull_request", classes=None, card=None,
              base_ref="main", head_ref="lot/1211-ci-impactes-contrat",
              nature_kw=None, mode="auto", **state_kw):
        state = self._state(classes if classes is not None else self._classes(), **state_kw)
        nature = {"version_only": False, "docs_only": False, "why": "", "files": []}
        nature.update(nature_kw or {})
        with mock.patch.object(plan, "classify", return_value=state), \
                mock.patch.object(plan, "nature", return_value=dict(nature)):
            return plan.make_plan(event, card if card is not None else {}, NOW,
                                  base_ref=base_ref, head_ref=head_ref, mode=mode)

    def _ran(self, p, driver="psycopg"):
        return sorted(m for ps in p["parts"][driver] for m in ps["modules"])

    def test_lot_branch_runs_impacted_and_contract_only(self):
        impacted = self.mods[0]
        p = self._plan(classes=self._classes(**{impacted: "src/ameesh/x.py::f"}))
        sel = p["selection"]
        self.assertEqual(sel["mode"], "impacted")
        self.assertEqual(sel["why"], "")
        self.assertEqual(sel["contract"], len(self.contract))
        expected = sorted(set(self.contract) | {impacted})
        self.assertEqual(self._ran(p), expected)
        self.assertEqual(sel["selected"], len(expected))
        self.assertEqual(sel["skipped"], len(self.mods) - len(expected))
        # le contrat garantit une matrice psycopg non vide : le check requis
        # « tests » ne peut pas verdir sans qu'une part ait tourné
        self.assertNotEqual(plan.outputs(p)["psycopg"], "[]")
        self.assertGreaterEqual(int(plan.outputs(p)["psycopg_parts"]), 1)

    def test_contract_runs_even_when_nothing_is_impacted(self):
        p = self._plan()
        self.assertEqual(self._ran(p), sorted(self.contract))
        self.assertEqual(plan.outputs(p)["psycopg_parts"], "1")

    def test_psql_keeps_the_pilot_subset(self):
        # le pilote psql d'une PR reste un sous-ensemble de la sélection
        impacted = self.pilot[0]
        p = self._plan(classes=self._classes(**{impacted: "x"}))
        expected = sorted((set(self.pilot) | {impacted})
                          & (set(self.contract) | {impacted}))
        self.assertEqual(self._ran(p, "psql"), expected)

    def test_psql_runs_a_triggered_always_module_outside_the_pilot(self):
        # #749 conservé sous le sélecteur : un module « always » déclenché
        # rejoint le pilote psql même hors tests/parts/pilote-psql.txt
        extra = self.contract[0]
        self.assertNotIn(extra, self.pilot)
        p = self._plan(classes=self._classes(**{extra: "always list (src/ameesh/runner.py)"}),
                       always=[extra])
        self.assertIn(extra, self._ran(p, "psql"))

    def test_any_doubt_runs_the_full_suite(self):
        cases = {
            "no map": {"map": None},
            "map unusable": {"map": {"commit": "c" * 40, "generated": NOW.isoformat(),
                                     "age_h": 0.0, "used": False, "problem": "map unreadable"}},
            "unsafe selection": {"phase2_safe": False, "lot_safe": False,
                                 "phase2_why": "3 changed block(s) without a test"},
            "empty contract": {"contract": []},
        }
        for why, state_kw in cases.items():
            with self.subTest(why):
                p = self._plan(**state_kw)
                self.assertEqual(p["selection"]["mode"], "full", why)
                self.assertTrue(p["selection"]["why"], why)
                self.assertEqual(self._ran(p), sorted(self.mods), why)

    def test_a_transverse_declaration_without_its_module_runs_the_full_suite(self):
        # #3954/B de cx3 : un module de contrat transverse déclaré mais absent
        # de tests/ sauterait le contrat en silence ; le plan le refuse.
        with mock.patch.dict(plan.transverse_contracts.CONTRACTS,
                             {"test_absent_3954": ("src/ameesh/x.py",)}, clear=True):
            p = self._plan()
        self.assertEqual(p["selection"]["mode"], "full")
        self.assertFalse(p["sure"])
        self.assertIn("test_absent_3954", " ".join(p["unsure"]))
        self.assertIn("contrat transverse", p["phase2_why"])
        self.assertEqual(self._ran(p), sorted(self.mods))

    def test_full_suite_on_main_release_and_integration(self):
        # une poussée sur main/release/integration garde la suite complète.
        p = self._plan(event="push")
        self.assertEqual(p["selection"]["mode"], "full")
        self.assertIn("integration", p["selection"]["why"])
        self.assertEqual(self._ran(p), sorted(self.mods))

    def test_full_request_on_a_lot_pull_request_runs_the_whole_suite(self):
        # #1343 : `ci-full-<sha>` (release, paquet, demande explicite) sur une
        # PR ordinaire garde la suite complète. #3749 : psycopg en entier,
        # psql sur sa liste pilote et tout le contrat.
        p = self._plan(event="pull_request", mode="full")
        self.assertEqual(p["selection"]["mode"], "full")
        self.assertEqual(self._ran(p), sorted(self.mods))
        self.assertEqual(self._ran(p, "psql"), sorted(set(self.pilot) | set(self.contract)))
        self.assertEqual([ps["of"] for ps in p["parts"]["psycopg"]],
                         _full_suite_parts("psycopg"))
        self.assertEqual([ps["of"] for ps in p["parts"]["psql"]],
                         _full_suite_parts("psql"))

    def test_release_pull_request_never_takes_the_impacted_selection(self):
        # R1 Codex (B1) : la carte est fraîche et sûre (les impactés sont
        # décidables), mais une PR dont la source ou la cible est release/**
        # ou integration/** garde la suite complète (canon §3) : #3749,
        # psycopg en entier, psql sur sa liste pilote et tout le contrat.
        cases = {
            "source release": {"base_ref": "main", "head_ref": "release/1.10.0"},
            "source integration": {"base_ref": "main", "head_ref": "integration/1.10.0"},
            "target release": {"base_ref": "release/1.10.0", "head_ref": "lot/fix"},
        }
        impacted = self.mods[0]
        for why, refs in cases.items():
            with self.subTest(why):
                p = self._plan(classes=self._classes(**{impacted: "src/ameesh/x.py::f"}),
                               **refs)
                self.assertTrue(p["phase2_safe"])
                self.assertEqual(p["selection"]["mode"], "full", why)
                self.assertIn("release/**", p["selection"]["why"])
                self.assertEqual(self._ran(p), sorted(self.mods), why)
                self.assertEqual(self._ran(p, "psql"),
                                 sorted(set(self.pilot) | set(self.contract)), why)
                self.assertEqual([ps["of"] for ps in p["parts"]["psycopg"]],
                                 _full_suite_parts("psycopg"))
                self.assertEqual([ps["of"] for ps in p["parts"]["psql"]],
                                 _full_suite_parts("psql"))
                self.assertEqual(plan.outputs(p)["code"], "true")

    def test_version_or_documentation_only_release_runs_the_full_suite(self):
        # R1 Codex : ni la voie version seule ni la voie documentation seule
        # ne sautent les parts d'une PR de release ; les mêmes PR vers main
        # (PR de lot) gardent leur voie rapide.
        for why, nature_kw in (("version", {"version_only": True, "why": "version seule"}),
                               ("documentation", {"docs_only": True, "why": "doc seule"})):
            with self.subTest(why):
                p = self._plan(head_ref="release/1.10.0", nature_kw=nature_kw)
                self.assertFalse(p["version_only"], why)
                self.assertFalse(p["docs_only"], why)
                self.assertIn("release/**", p["why"])
                self.assertEqual(self._ran(p), sorted(self.mods), why)
                self.assertEqual(plan.outputs(p)["code"], "true")
                lot = self._plan(head_ref="lot/x", nature_kw=nature_kw)
                self.assertEqual(lot["parts"]["psycopg"], [], why)
                self.assertEqual(plan.outputs(lot)["code"], "false", why)

    def test_fallback_of_a_release_pull_request_keeps_the_full_suite(self):
        # R1 Codex : « y compris fallback » — le repli d'une PR de release ne
        # reprend pas le découpage d'une PR de lot. #3749 : la suite complète
        # est psycopg en entier et la liste pilote sous psql.
        p = plan.fallback_plan("pull_request", "boom", base_ref="main",
                               head_ref="release/1.10.0")
        self.assertEqual(json.loads(plan.outputs(p)["psycopg"]),
                         list(range(1, plan.PARTS["psycopg"][1] + 1)))
        self.assertEqual(json.loads(plan.outputs(p)["psql"]),
                         list(range(1, plan.PARTS["psql"][1] + 1)))
        self.assertTrue(all(ps.get("pilot_list") for ps in p["parts"]["psql"]))
        self.assertIn("release/**", p["selection"]["why"])
        lot = plan.fallback_plan("pull_request", "boom", base_ref="main", head_ref="lot/x")
        self.assertEqual(json.loads(plan.outputs(lot)["psycopg"]),
                         list(range(1, plan.PARTS["psycopg"][1] + 1)))
        self.assertEqual(json.loads(plan.outputs(lot)["psql"]),
                         list(range(1, plan.PARTS["psql"][1] + 1)))
        self.assertTrue(any(ps.get("pilot_list") for ps in lot["parts"]["psql"]))

    def test_github_refs_default_to_the_environment(self):
        # le workflow passe --base-ref/--head-ref ; à défaut (appel direct),
        # GITHUB_BASE_REF et GITHUB_HEAD_REF portent le contexte (L1211).
        env = {"GITHUB_BASE_REF": "main", "GITHUB_HEAD_REF": "release/1.10.0"}
        with mock.patch.dict("os.environ", env):
            p = self._plan(base_ref=None, head_ref=None)
        self.assertEqual(p["refs"], {"base": "main", "head": "release/1.10.0"})
        self.assertEqual(p["selection"]["mode"], "full")
        self.assertIn("release/**", p["release"])

    def test_summary_names_the_selection(self):
        p = self._plan(classes=self._classes(**{self.mods[0]: "src/ameesh/x.py::f"}))
        text = plan.summary(p)
        self.assertIn("lot branch (L1211)", text)
        self.assertIn(f"{len(self.contract)} contract module(s)", text)
        full = self._plan(phase2_safe=False, lot_safe=False,
                          phase2_why="changed block(s) without a test")
        text = plan.summary(full)
        self.assertIn("full suite", text)
        self.assertIn("changed block(s) without a test", text)

    def test_summary_separates_proven_importers_from_candidates(self):
        # #1339, B2 résiduel : seuls les importateurs qui prouvent le
        # chargement sont annoncés couverts ; un candidat paresseux à côté d'un
        # fichier non couvert est nommé, sans faire passer le plan pour sûr.
        p = self._plan(importers={"src/ameesh/x.py": [self.mods[0]]},
                       certified={"src/ameesh/x.py": [self.mods[0]]})
        text = plan.summary(p)
        self.assertIn("unmeasured blocks covered by their importers", text)
        unknown = self._plan(phase2_safe=False, lot_safe=False,
                             phase2_why="selection unsafe",
                             importers={"src/ameesh/y.py": [self.mods[0]]},
                             uncovered=["src/ameesh/y.py"])
        text = plan.summary(unknown)
        self.assertIn("no test is guaranteed to load", text)
        self.assertIn(f"candidates: `{self.mods[0]}`", text)

    def test_parts_shrink_to_the_estimate(self):
        # Liste fixe (#1800) : le témoin porte sur la formule de découpage
        # (235 modules × 2 s = 470 s -> deux parts), pas sur l'inventaire du
        # dépôt, qui grandit à chaque paquet et ferait basculer l'attente.
        # Noms synthétiques : test_count, qui lit un fichier réel, est neutralisé.
        mods = [f"test_fixe_{i:03d}" for i in range(235)]

        def card(seconds):
            return {"modules": {m: {"durees": {"psycopg": seconds, "psql": seconds}}
                                for m in mods}}
        with mock.patch.object(plan.parts, "test_count", lambda module, tests=None: 1):
            self.assertEqual(plan.lot_part_count("psycopg", mods[:10], card(1.0)), 1)
            self.assertEqual(plan.lot_part_count("psycopg", mods, card(2.0)), 2)
            self.assertGreaterEqual(plan.lot_part_count("psycopg", mods, card(5.0)), 4)

    def test_selected_matrix_never_exceeds_the_parts_cap(self):
        # un lot très large (60 s par module) reste dans PARTS : quatre parts
        # psycopg et trois parts psql au plus, comme la suite complète
        classes = self._classes(**{m: "x" for m in self.mods})
        p = self._plan(classes=classes, card={"modules": {
            m: {"durees": {"psycopg": 60.0, "psql": 60.0}} for m in self.mods}})
        self.assertEqual(p["selection"]["mode"], "impacted")
        self.assertEqual({ps["of"] for ps in p["parts"]["psycopg"]}, {4})
        self.assertEqual({ps["of"] for ps in p["parts"]["psql"]}, {3})
        self.assertEqual(self._ran(p), sorted(self.mods))


def _cases():
    """Example tests, out of reach of test discovery."""

    class Impacted(unittest.TestCase):
        def test_ok(self):
            pass

        def test_fail(self):
            self.fail("no")

    class Rest(unittest.TestCase):
        def test_ok(self):
            pass

    return Impacted, Rest


class ProvisionalGreenTest(unittest.TestCase):
    def _run(self, impacted: tuple, *cases):
        d = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d)
        flux = parts.Flow(d / "flux.jsonl", {"part": "psycopg 1/1"})
        cls = type("_R", (parts._Result,), {"flow": flux, "classes": {},
                                              "impactes": impacted})
        suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(c)
                                   for c in cases)
        unittest.TextTestRunner(stream=io.StringIO(), resultclass=cls).run(suite)
        flux.close()
        return [json.loads(x) for x in (d / "flux.jsonl").read_text().splitlines()]

    def test_event_after_the_impacted_modules(self):
        impacted, rest = _cases()
        module = __name__.rsplit(".", 1)[-1]
        # same module for both classes here: the event comes after the run
        lines = self._run((module,), impacted, rest)
        events = [x for x in lines if x["type"] == "impacted"]
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0]["statut"], events[0]["rouges"]), ("rouge", [module]))
        self.assertEqual(lines[-1]["type"], "impacted")

    def test_broken_setupclass_is_red_in_its_module(self):
        # L161f review (1): « setUpClass (tests.test_x.C) » was put in module « tests »,
        # a provisional green and a P3 leak for a migration breaking a P1 setUpClass
        class Broken(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                raise RuntimeError("migration")

            def test_never(self):
                pass

        _, rest = _cases()
        module = __name__.rsplit(".", 1)[-1]
        lines = self._run((module,), Broken, rest)
        error = next(x for x in lines if x["type"] == "test" and x["statut"] == "erreur")
        self.assertEqual(error["module"], module)
        event = next(x for x in lines if x["type"] == "impacted")
        self.assertEqual((event["statut"], event["rouges"]), ("rouge", [module]))

    def test_module_of_fixture_and_import_errors(self):
        from unittest.suite import _ErrorHolder
        module = __name__.rsplit(".", 1)[-1]
        for description in (f"setUpModule ({__name__})", f"tearDownModule ({__name__})",
                            f"setUpClass ({__name__}.C)", f"tearDownClass ({__name__}.C)",
                            f"setUpClass ({__name__}._cases.<locals>.Impacted)"):
            with self.subTest(description=description):
                self.assertEqual(parts._module_of(_ErrorHolder(description)), module)
        failed = unittest.defaultTestLoader.loadTestsFromName("tests.test_absent_l161f")
        self.assertEqual(parts._module_of(next(iter(failed))), "test_absent_l161f")

    def test_no_impacted_module_green_at_once(self):
        _, rest = _cases()
        lines = self._run((), rest)
        self.assertEqual(lines[0]["type"], "impacted")
        self.assertEqual(lines[0]["statut"], "vert")

    def test_no_event_without_a_plan(self):
        _, rest = _cases()
        self.assertEqual([x["type"] for x in self._run(None, rest)], ["test"])


class LeaksTest(unittest.TestCase):
    def _streams(self, d: pathlib.Path, green: str = "vert", second_part: bool = True):
        def line(**kw):
            return json.dumps({"v": 1, **kw}) + "\n"
        (d / "a.jsonl").write_text(
            line(type="debut", part="psycopg 1/2", ts="2026-10-11T12:00:00Z")
            + line(type="impacted", part="psycopg 1/2", statut=green, ts="2026-10-11T12:00:40Z")
            + line(type="test", part="psycopg 1/2", module="test_p1", statut="echec", test="t1")
            + line(type="test", part="psycopg 1/2", module="test_p3", statut="erreur", test="t3"))
        files = [str(d / "a.jsonl")]
        if second_part:
            (d / "b.jsonl").write_text(
                line(type="debut", part="psycopg 2/2", ts="2026-10-11T12:00:05Z")
                + line(type="impacted", part="psycopg 2/2", statut="vert",
                       ts="2026-10-11T12:01:00Z"))
            files.append(str(d / "b.jsonl"))
        return files

    def setUp(self):
        self.d = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d)
        self.plan = {"event": "pull_request", "map": {"commit": "c", "used": True},
                     "sure": True, "phase2_safe": False,
                     "classes": {"test_p1": ["P1", "x"], "test_p3": ["P3", ""]},
                     "parts": {"psycopg": [{"label": "psycopg 1/2"}, {"label": "psycopg 2/2"}],
                               "psql": []}}

    def test_leak_and_provisional_green(self):
        record = plan.leaks(self.plan, self._streams(self.d))
        self.assertEqual(record["failed"], ["test_p1", "test_p3"])
        self.assertEqual([x["module"] for x in record["leaks"]], ["test_p3"])
        self.assertEqual(record["provisional_green"], {"status": "green", "after_s": 60.0})
        self.assertIn("**leaks**", plan.leaks_summary(record))

    def test_red_or_incomplete(self):
        record = plan.leaks(self.plan, self._streams(self.d, green="rouge"))
        self.assertEqual(record["provisional_green"]["status"], "red")
        record = plan.leaks(self.plan, self._streams(self.d, second_part=False))
        self.assertEqual(record["provisional_green"],
                         {"status": "incomplete", "missing": ["psycopg 2/2"]})

    def test_tally(self):
        leak = {"event": "pull_request", "map_used": True, "leaks": [{"module": "m"}]}
        clean = {"event": "pull_request", "map_used": True, "leaks": [], "sure": True}
        unmapped = {"event": "pull_request", "map_used": False, "leaks": [{"module": "m"}]}
        out = plan.tally([leak, clean, clean, clean, unmapped])
        self.assertEqual(out["pull_request"]["runs"], 4)
        self.assertEqual(out["pull_request"]["per_50"], 12.5)
        self.assertEqual(out["not_counted"], 1)


def _key_column(line: str) -> int:
    """Column of the YAML key: indentation, then an optional `- `."""
    col = len(line) - len(line.lstrip(" "))
    if line[col:].startswith("- "):
        col += 2
        while col < len(line) and line[col] == " ":
            col += 1
    return col


def _block_header(token: str) -> bool:
    """Does the token after `key:` open a block scalar?

    `|`, `>`, their indentation and chomping indicators in either order
    (`|2`, `>-2`, `|-2`) and a trailing comment (`| # ...`) are all headers,
    never an inline body (#1247).
    """
    token = token.split("#", 1)[0].strip()
    return bool(token) and token[0] in "|>" and all(
        char.isdigit() or char in "+-" for char in token[1:])


def _block_body(lines: list[str], i: int, key_col: int) -> tuple[str, int]:
    """Body of the block scalar opened on line `i`, and the next line."""
    body, j = [], i + 1
    while j < len(lines):
        nxt = lines[j]
        if nxt.strip() and len(nxt) - len(nxt.lstrip(" ")) <= key_col:
            break
        body.append(nxt)
        j += 1
    return "\n".join(body), j


def _node_properties(token: str) -> tuple[str | None, str]:
    """Strip the node properties that may precede a `run:` value.

    A plain scalar never starts with `&` or `!`: `&name` anchors the node and
    `!tag` / `!!tag` types it, in either order. Both must be seen through, so
    that the block header behind them opens the body and the anchor is
    registered (#1247).
    """
    name = None
    while token[:1] in ("&", "!"):
        if token[0] == "&":
            match = re.match(r"^&([A-Za-z0-9_.-]+)\s*", token)
            if not match:
                break
            name = match.group(1)
        else:
            match = re.match(r"^![^\s]*\s*", token)
            if not match:
                break
        token = token[match.end():]
    return name, token


def _declaration(lines: list[str], i: int) -> tuple[str | None, str, int]:
    """Anchor declared on line `i`: `(name, value, next line)`.

    `(None, "", next)` when the line declares none. A block body is skipped
    whole, so that shell text inside a scalar is never read as structure and a
    declaration only reaches the aliases that follow it.
    """
    col = _key_column(lines[i])
    match = re.match(r"^[^\s:#][^:#]*:[ \t]*(?P<rest>.*)$", lines[i][col:])
    if not match:
        return None, "", i + 1
    name, rest = _node_properties(match.group("rest").strip())
    if _block_header(rest):
        value, j = _block_body(lines, i, col)
        return (name, value, j) if name else (None, "", j)
    if not name:
        return None, "", i + 1
    # #1247 R2: a flow scalar may go on over the more indented lines, and an
    # empty value is a node on the next lines; the anchor holds all that text,
    # which is still read as structure.
    more, _ = _block_body(lines, i, col)
    return name, "\n".join(filter(None, (rest, more))), i + 1


def _run_bodies(text: str) -> list[str]:
    """Text of every `run:` body in tests.yml.

    Covers inline scalars, block scalars (`|`, `>`, `|2`, `>-2`, header
    comment), the list form (`- run:`), node properties (`&anchor`, `!tag`)
    and aliases (`run: *anchor`). An alias is resolved at its point of use:
    a declaration that follows it never replaces the one it names (#1247).
    No body may escape the gate witness that forbids `github.base_ref` /
    `github.head_ref` inside a `run:` block.
    """
    lines = text.splitlines()
    anchors: dict[str, str] = {}
    bodies: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        col = _key_column(line)
        stripped = line[col:]
        if not stripped.startswith("run:"):
            name, value, i = _declaration(lines, i)
            if name:
                anchors[name] = value
            continue
        token = stripped[len("run:"):].strip()
        name, token = _node_properties(token)
        if token.startswith("*"):
            alias = token[1:].split("#", 1)[0].strip()
            if alias not in anchors:
                # #1247 R2: an anchor the scanner cannot read (sequence item,
                # flow node) fails the witness instead of dropping the body.
                raise ValueError(f"run: *{alias}: anchor not readable by the witness")
            bodies.append(anchors[alias])
            i += 1
            continue
        if token and not _block_header(token):
            # #1247 R2: a plain or quoted scalar may go on over the more
            # indented lines.
            more, j = _block_body(lines, i, col)
            body = "\n".join(filter(None, (token, more)))
            if name:
                anchors[name] = body
            bodies.append(body)
            i = j
            continue
        body, j = _block_body(lines, i, col)
        if name:
            anchors[name] = body
        bodies.append(body)
        i = j
    return bodies


_BRANCH_REF = re.compile(r"github\.(base|head)_ref")
# #1559: the value YAML reads is the same expression whatever the spacing
# inside `${{ }}` and whether the scalar is quoted (`"${{ … }}"`,
# `'${{ … }}'`), and a tab or extra spaces after the colon still open the
# mapping value. Everything else stays an escape (fail closed).
_BRANCH_REF_EXPR = r"\$\{\{\s*github\.(?:base|head)_ref\s*\}\}"
_BRANCH_REF_ENV = re.compile(
    "^[A-Z][A-Z0-9_]*:[ \t]+(?:"
    + _BRANCH_REF_EXPR
    + '|"' + _BRANCH_REF_EXPR + '"'
    + "|'" + _BRANCH_REF_EXPR + "'"
    + r")[ \t]*(?:#.*)?$")
_ANY_KEY = re.compile(
    r"""^(?:"(?:[^"\\]|\\.)*"|'(?:[^']|'')*'|[^\s#'"{\[][^#]*?)\s*:(?:\s+|$)(?P<rest>.*)$""")
_BARE_KEY = re.compile(r"^[A-Za-z0-9_.-]+:(?:\s+&[A-Za-z0-9_.-]+)?\s*(?:#.*)?$")
_ITEM = re.compile(r"^(?:&[A-Za-z0-9_.-]+\s*)?(?:#.*)?$|^[A-Za-z0-9_.-]+:(?:\s|$)")


def _dash_column(line: str) -> int | None:
    col = len(line) - len(line.lstrip(" "))
    return col if line[col:].startswith("- ") or line[col:] == "-" else None


def _left_open(content: str) -> bool:
    """Does a quoted scalar or a flow collection go on past this line?"""
    # Fail closed on an opening delimiter after whitespace or a YAML flow
    # separator, whatever key, explicit value marker or property precedes it.
    # A plain scalar with an unmatched delimiter may be refused; permitting it
    # could hide a quoted/flow continuation whose indentation YAML ignores.
    depth, quote, i = 0, None, 0
    while i < len(content):
        char = content[i]
        start = i == 0 or content[i - 1].isspace() or content[i - 1] in "[{,:"
        if quote == '"':
            if char == "\\":
                i += 1
            elif char == '"':
                quote = None
        elif quote == "'":
            if content[i:i + 2] == "''":
                i += 1
            elif char == "'":
                quote = None
        elif char == "#" and (i == 0 or content[i - 1] in " \t"):
            break
        elif char in "\"'" and (start or depth):
            quote = char
        elif char in "{[" and (start or depth):
            depth += 1
        elif char in "}]" and depth:
            depth -= 1
        i += 1
    return quote is not None or depth > 0


def _structural_lines(lines: list[str]) -> set[int]:
    """Lines that are YAML structure, outside every block scalar body."""
    keep, i = set(), 0
    while i < len(lines):
        keep.add(i)
        dash, col = _dash_column(lines[i]), _key_column(lines[i])
        content = lines[i][col:]
        match = _ANY_KEY.match(content)
        _, rest = _node_properties((match.group("rest") if match else content).strip())
        if _block_header(rest):
            _, i = _block_body(lines, i, col if match or dash is None else dash)
        else:
            i += 1
    return keep


def _under_env(lines: list[str], k: int, structural: set[int]) -> bool:
    """Is line `k` a key of an `env:` mapping, itself under mapping keys only?

    Every ancestor must be a bare block key (or a sequence item that opens a
    mapping): a scalar on the way (block, quoted, flow, plain) would make the
    line its text, not an environment variable.
    """
    col, first = _key_column(lines[k]), True
    for j in range(k - 1, -1, -1):
        line = lines[j]
        if j not in structural or not line.strip() or line.lstrip().startswith("#"):
            continue
        dash, key = _dash_column(line), _key_column(line)
        # #1559: an anchor or a tag before the key (`- &s env:`) does not
        # change the mapping; only node properties are seen through, a scalar
        # on the way still fails the witness.
        _, content = _node_properties(line[key:])
        if key < col:
            if not _BARE_KEY.match(content) or (
                    first and content.split(":", 1)[0] != "env"):
                return False
        elif dash is not None and dash < col:
            if first or not _ITEM.match(content):
                return False
        else:
            continue
        first, col = False, key if dash is None else dash
    return not first


def _branch_ref_escapes(text: str) -> list[str]:
    """Lines where a branch name could reach something other than `env:`.

    #1247 R2: `_run_bodies` does not parse every form (quoted key, `run :`,
    flow mapping), so the witness also closes on the structure: a branch name
    is only allowed as `NAME: ${{ github.<x>_ref }}` in a real `env:`
    mapping, and no quoted scalar or flow collection may go on past its line
    (a YAML loader ignores indentation inside them).
    """
    lines = text.splitlines()
    structural = _structural_lines(lines)
    escapes = []
    for k, line in enumerate(lines):
        col = _key_column(line)
        if k in structural and _left_open(line[col:]):
            escapes.append(line)
        elif _BRANCH_REF.search(line) and not (
                k in structural and _branch_ref_allowed(lines, k, structural)):
            escapes.append(line)
    return escapes


_RUNNER_SELECTION = re.compile(
    r"^runs-on:[ \t]*(?:"
    r"\$\{\{.*\}\}"                       # valeur nue
    r"|\"\$\{\{.*\}\}\""                  # valeur a guillemets doubles
    r"|'\$\{\{.*\}\}'"                    # valeur a guillemets simples
    r")[ \t]*(?:#.*)?$")
_BRANCH_REF_COMPARED = re.compile(
    r"startsWith\(\s*github\.(?:base|head)_ref\s*,\s*'(?:[^',()]|'')*'\)")


def _job_level(lines: list[str], k: int, structural: set[int]) -> bool:
    """Is line `k` a direct key of a job mapping, under `jobs:`?

    #1563: the `runs-on:` exemption is job-level only. A `runs-on:` nested in
    a step mapping, a strategy or any other level is not the label GitHub
    reads for a job; it stays text and the witness keeps refusing an
    interpolation there (fail closed).
    """
    col, ancestors = _key_column(lines[k]), []
    for j in range(k - 1, -1, -1):
        line = lines[j]
        if j not in structural or not line.strip() or line.lstrip().startswith("#"):
            continue
        dash, key = _dash_column(line), _key_column(line)
        if dash is not None and dash < col:
            return False                     # un item de sequence sur le chemin
        if key < col:
            if not _BARE_KEY.match(line[key:]):
                return False
            ancestors.append(j)
            col = key
            if len(ancestors) == 2:
                break
    if len(ancestors) != 2:
        return False
    _job, jobs = ancestors
    return _key_column(lines[jobs]) == 0 and lines[jobs].startswith("jobs:")


def _branch_ref_allowed(lines: list[str], k: int, structural: set[int]) -> bool:
    """May line `k` name a branch instead of interpolating it?

    An `env:` value (#1237/#1247), or a job-level `runs-on:` expression: GitHub
    reads the runner label itself, never a shell, and every reference must be
    compared to a literal (`startsWith`) — a bare `${{ github.head_ref }}`
    stays an escape. (#1468: the priority pool reads the source and target
    branches of a PR; a PR author can name a branch `integration/...` and pick
    the pool, so the comparison is a priority choice, not a gate bypass.)

    The compared literal is a single argument: dev4's N, a quoted literal that
    spans arguments of a `format()` (`'…startsWith(github.head_ref, ', …`)
    must not absolve the interpolated reference that follows.

    #1563: the `runs-on:` exemption is bound to the job level (`_job_level`)
    and reads the YAML spellings GitHub reads for the value: bare or quoted
    (`"${{ … }}"`, `'${{ … }}'`), spaces after `startsWith(` and around the
    literal, and `''` for an apostrophe inside a compared literal. Two forms
    stay documented fail-closed limits: a `${{` opened on one line and closed
    on another (`_left_open` refuses the unclosed flow), and a single-quoted
    value whose compared literal uses the `''` escape, which the witness does
    not decode.
    """
    line = lines[k]
    col = _key_column(line)
    if _dash_column(line) is not None:
        return False
    if _BRANCH_REF_ENV.match(line[col:]) and _under_env(lines, k, structural):
        return True
    if _RUNNER_SELECTION.match(line[col:]) is None:
        return False
    if not _job_level(lines, k, structural):
        return False
    return not _BRANCH_REF.search(_BRANCH_REF_COMPARED.sub("", line[col:]))


class RunBodiesTest(unittest.TestCase):
    """#1247: no exotic `run:` body escapes the merge-gate witness."""

    def test_flow_scalar_continuation_lines_are_read(self):
        # #1247 R2: plain and double-quoted scalars go on over the more
        # indented lines; `X: ...` is legal inside the quoted one.
        text = textwrap.dedent("""\
            jobs:
              a:
                steps:
                  - run: echo start
                      "${{ github.base_ref }}"
                  - run: "echo start
                      HEAD: ${{ github.head_ref }}"
                    name: next
            """)
        bodies = _run_bodies(text)
        self.assertEqual(2, len(bodies))
        self.assertIn("github.base_ref", bodies[0])
        self.assertIn("github.head_ref", bodies[1])
        self.assertNotIn("name:", bodies[1])

    def test_anchor_whose_value_is_on_the_next_lines(self):
        text = textwrap.dedent("""\
            x-command: &cmd
              echo "${{ github.base_ref }}"
            jobs:
              a:
                steps:
                  - run: *cmd
            """)
        bodies = _run_bodies(text)
        self.assertEqual(1, len(bodies))
        self.assertIn("github.base_ref", bodies[0])

    def test_unreadable_anchor_fails_the_witness(self):
        # #1247 R2: an anchor on a sequence item is not read; its alias must
        # not be dropped silently.
        text = textwrap.dedent("""\
            x-commands:
              - &cmd |
                echo "${{ github.base_ref }}"
            jobs:
              a:
                steps:
                  - run: *cmd
            """)
        with self.assertRaises(ValueError):
            _run_bodies(text)

    def test_branch_ref_only_in_a_real_env_mapping(self):
        # #1247 R2 (B1 mesh-design, qa01): the form of the line is not enough,
        # it must be a key of a real `env:` mapping; and no quoted scalar or
        # flow collection may go on past its line (a YAML loader reads it
        # whatever the indentation of the next lines).
        steps = "jobs:\n  a:\n    steps:\n"
        escapes = {
            "quoted run key": '      - "run": |\n'
                              '          BASE_REF: ${{ github.base_ref }}\n',
            "space before colon": '      - run : |\n'
                                  '          HEAD_REF: ${{ github.head_ref }}\n',
            "flow mapping": '      - {run: "echo start\n'
                            '          BASE_REF: ${{ github.base_ref }}"}\n',
            "quoted scalar over env": '      - name: s\n        run: "echo\n'
                                      '        env:\n'
                                      '          BASE_REF: ${{ github.base_ref }}"\n',
            "flow mapping over env": '      - {name: s, run: "echo\n    env:\n'
                                     '      BASE_REF: ${{ github.base_ref }}"}\n',
            "env inside a block": '      - "run": |\n          env:\n'
                                  '            BASE_REF: ${{ github.base_ref }}\n',
            "block sequence item": '      - |\n        env:\n'
                                   '          BASE_REF: ${{ github.base_ref }}\n',
            "explicit key": '      - ? run\n        : |\n'
                            '          BASE_REF: ${{ github.base_ref }}\n',
            "inline mention": '      - {name: x, run: "echo ${{ github.base_ref }}"}\n',
            "run value": '      - run: ${{ github.head_ref }}\n',
        }
        for name, step in escapes.items():
            with self.subTest(name=name):
                self.assertNotEqual([], _branch_ref_escapes(steps + step))
        allowed = {
            "step env, comments": '      - name: s\n        env:  # x\n'
                                  '          BASE_REF: ${{ github.base_ref }}  # y\n'
                                  '        run: echo "$BASE_REF"\n',
            "anchored step": '      - &s\n        name: s\n        env:\n'
                             '          HEAD_REF: ${{ github.head_ref }}\n',
            "env first in the item": '      - env:\n'
                                     '          BASE_REF: ${{ github.base_ref }}\n',
            # #1559: YAML reads the same env value under these legitimate
            # spellings; only the semantics must count, not the exact form.
            "quoted value": '      - name: s\n        env:\n'
                            '          HEAD_REF: "${{ github.head_ref }}"\n'
                            '        run: echo "$HEAD_REF"\n',
            "single quoted value": '      - name: s\n        env:\n'
                                   "          BASE_REF: '${{ github.base_ref }}'\n"
                                   '        run: echo "$BASE_REF"\n',
            "no inner spaces": '      - name: s\n        env:\n'
                               '          HEAD_REF: ${{github.head_ref}}\n'
                               '        run: echo "$HEAD_REF"\n',
            "two spaces after colon": '      - name: s\n        env:\n'
                                      '          BASE_REF:  ${{ github.base_ref }}\n'
                                      '        run: echo "$BASE_REF"\n',
            "anchor before the env key": '      - &s env:\n'
                                         '          BASE_REF: ${{ github.base_ref }}\n'
                                         '        run: echo "$BASE_REF"\n',
        }
        for name, step in allowed.items():
            with self.subTest(name=name):
                self.assertEqual([], _branch_ref_escapes(steps + step))
        # #1559: the same values, read by YAML in a `run:` body instead of an
        # `env:` value, stay escapes: the witness follows the semantics.
        for name, body in {
            "quoted": 'BASE_REF: "${{ github.base_ref }}"\n',
            "single quoted": "BASE_REF: '${{ github.base_ref }}'\n",
            "no inner spaces": 'HEAD_REF: ${{github.head_ref}}\n',
            "two spaces after colon": 'BASE_REF:  ${{ github.base_ref }}\n',
        }.items():
            with self.subTest(run=name):
                self.assertNotEqual([], _branch_ref_escapes(
                    steps + '      - run: |\n          ' + body))
        self.assertNotEqual([], _branch_ref_escapes(
            steps + '      - &s run: |\n          BASE_REF: ${{ github.base_ref }}\n'))
        self.assertEqual([], _branch_ref_escapes(
            "env:\n  BASE_REF: ${{ github.base_ref }}\n"
            "jobs:\n  a:\n    env:\n      HEAD_REF: ${{ github.head_ref }}\n"))

    def test_runner_selection_may_compare_the_branch(self):
        # #1247 x #1468: le pool prioritaire lit les branches source et cible ;
        # dans un `runs-on:`, GitHub évalue l'étiquette lui-même (aucun shell),
        # mais une interpolation nue y reste une évasion.
        compared = ("jobs:\n  a:\n    runs-on: ${{ startsWith(github.head_ref, "
                    "'refs/heads/integration/') && 'pool' || 'ci' }}\n")
        self.assertEqual([], _branch_ref_escapes(compared))
        for bare in ("    runs-on: ${{ github.head_ref }}\n",
                     "    runs-on: ${{ format('{0}', github.base_ref) }}\n",
                     "    runs-on: ${{ startsWith(github.head_ref, vars.X) }}\n",
                     "    runs-on: ${{ format('x{0}startsWith(github.head_ref, ',"
                     " github.head_ref, ')') }}\n",
                     "    runs-on: \"${{ github.head_ref }}"
                     "\n      run: echo ok\"\n"):
            with self.subTest(bare=bare):
                self.assertNotEqual([], _branch_ref_escapes("jobs:\n  a:\n" + bare))

    def test_runner_selection_is_job_level_and_reads_yaml_spellings(self):
        # #1563: l'exemption suit les formes YAML que GitHub lit pour un vrai
        # `runs-on:` de job, et rien d'autre.
        compared = "startsWith(github.head_ref, 'refs/heads/integration/')"
        for name, line in {
            "nue": "    runs-on: ${{ %s && 'pool' || 'ci' }}\n" % compared,
            "guillemets doubles": (
                "    runs-on: \"${{ %s && 'pool' || 'ci' }}\"\n" % compared),
            "espace apres startsWith(": (
                "    runs-on: ${{ startsWith( github.head_ref, "
                "'refs/heads/integration/') && 'pool' || 'ci' }}\n"),
            "apostrophe doublee": (
                "    runs-on: ${{ startsWith(github.head_ref, "
                "'refs/heads/it''s/') && 'pool' || 'ci' }}\n"),
        }.items():
            with self.subTest(forme=name):
                self.assertEqual([], _branch_ref_escapes("jobs:\n  a:\n" + line))
        # La meme ligne nichee dans un mapping d'etape ou une matrice n'est pas
        # le `runs-on:` d'un job : elle reste une evasion (echec ferme).
        for name, text in {
            "mapping d'etape": ("jobs:\n  a:\n    steps:\n      - name: s\n"
                                "        runs-on: ${{ %s && 'pool' || 'ci' }}\n"
                                % compared),
            "matrice": ("jobs:\n  a:\n    strategy:\n      matrix:\n"
                        "        runs-on: ${{ %s && 'pool' || 'ci' }}\n" % compared),
        }.items():
            with self.subTest(hors_job=name):
                self.assertNotEqual([], _branch_ref_escapes(text))
        # Une expression `${{` ouverte sur une ligne et fermee sur une autre
        # reste refusee : limite d'echec ferme documentee sur _branch_ref_allowed.
        self.assertNotEqual([], _branch_ref_escapes(
            "jobs:\n  a:\n    runs-on: ${{\n"
            "      startsWith(github.head_ref, 'refs/heads/integration/') }}\n"))

    def test_properties_cannot_hide_a_multiline_scalar_over_env(self):
        # YAML ignores indentation inside quotes, even after an anchor/tag.
        # The apparent root env below is still text of the run scalar.
        for key in ("run", '"run"', "run "):
            for properties in ("&cmd", "!!str", "!<tag:yaml.org,2002:str>",
                               "&cmd !!str"):
                text = (f"jobs:\n  a:\n    steps:\n      - {key}: {properties} \"echo\n"
                        "env:\n  BASE_REF: ${{ github.base_ref }}\nend\"\n")
                with self.subTest(key=key, properties=properties):
                    self.assertNotEqual([], _branch_ref_escapes(text))

    def test_explicit_mapping_value_properties_cannot_hide_a_scalar(self):
        # The YAML value starts after `:`, independently of the explicit key.
        # Quoted scalar continuation ignores the apparent root env's indent.
        for key in ("run", '"run"'):
            for properties in ("&cmd", "!!str", "!<tag:yaml.org,2002:str>",
                               "&cmd !!str"):
                text = (f"jobs:\n  a:\n    steps:\n      - ? {key}\n"
                        f"        : {properties} \"echo\n"
                        "env:\n  BASE_REF: ${{ github.base_ref }}\nend\"\n")
                with self.subTest(key=key, properties=properties):
                    self.assertNotEqual([], _branch_ref_escapes(text))

    def test_block_headers_indicators_and_comment(self):
        text = textwrap.dedent("""\
            jobs:
              a:
                steps:
                  - run: |2
                      echo "${{ github.base_ref }}"
                  - run: >-2
                      echo "${{ github.head_ref }}"
                  - run: |- # header comment
                      echo ok
                  - run: echo inline
            """)
        bodies = _run_bodies(text)
        self.assertEqual(4, len(bodies))
        self.assertIn("github.base_ref", bodies[0])
        self.assertIn("github.head_ref", bodies[1])
        self.assertEqual("echo ok", bodies[2].strip())
        self.assertEqual("echo inline", bodies[3])

    def test_list_form_does_not_swallow_the_next_key(self):
        text = textwrap.dedent("""\
            steps:
              - run: |2
                  echo "${{ github.base_ref }}"
                name: suite
            """)
        bodies = _run_bodies(text)
        self.assertEqual(1, len(bodies))
        self.assertIn("github.base_ref", bodies[0])
        self.assertNotIn("name:", bodies[0])

    def test_alias_resolves_the_anchored_block(self):
        text = textwrap.dedent("""\
            x-command: &cmd |2
              echo "${{ github.base_ref }}"
            jobs:
              a:
                steps:
                  - run: *cmd
                  - run: echo inline
            """)
        bodies = _run_bodies(text)
        self.assertEqual(2, len(bodies))
        self.assertIn("github.base_ref", bodies[0])
        self.assertEqual("echo inline", bodies[1])

    def test_alias_of_an_inline_anchor_keeps_the_quoted_hash(self):
        text = textwrap.dedent("""\
            x-command: &cmd "echo # ${{ github.base_ref }}"
            jobs:
              a:
                steps:
                  - run: *cmd
            """)
        bodies = _run_bodies(text)
        self.assertEqual(1, len(bodies))
        self.assertIn("github.base_ref", bodies[0])

    def test_anchor_on_the_run_line_still_opens_the_block(self):
        # #1247 R1 (P2): `- run: &cmd |` both declares the anchor and opens
        # the block; the body must not be taken for the inline value `&cmd |`.
        text = textwrap.dedent("""\
            jobs:
              a:
                steps:
                  - run: &cmd |
                      echo "${{ github.base_ref }}"
                  - run: *cmd
            """)
        bodies = _run_bodies(text)
        self.assertEqual(2, len(bodies))
        self.assertIn("github.base_ref", bodies[0])
        self.assertIn("github.base_ref", bodies[1])

    def test_tag_before_the_block_header_still_opens_the_block(self):
        # #1247: a tag is the same class as an anchor (`run: !!str |`).
        text = textwrap.dedent("""\
            jobs:
              a:
                steps:
                  - run: !!str |
                      echo "${{ github.base_ref }}"
            """)
        bodies = _run_bodies(text)
        self.assertEqual(1, len(bodies))
        self.assertIn("github.base_ref", bodies[0])

    def test_alias_resolves_at_its_point_of_use(self):
        # #1247 R1: an alias names the declaration that precedes it; the last
        # declaration of the file must not replace it retroactively.
        text = textwrap.dedent("""\
            x-first: &cmd |
              echo "${{ github.base_ref }}"
            jobs:
              a:
                steps:
                  - run: *cmd
            x-last: &cmd |
              echo benign
            """)
        bodies = _run_bodies(text)
        self.assertEqual(1, len(bodies))
        self.assertIn("github.base_ref", bodies[0])
        self.assertNotIn("benign", bodies[0])

    def test_only_run_keys_are_read(self):
        text = textwrap.dedent("""\
            jobs:
              a:
                runs-on: ubuntu-latest
                run-name: build
                steps:
                  - run: echo hi
            """)
        self.assertEqual(["echo hi"], _run_bodies(text))


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.text = (REPO / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")

    def test_plan_job_reads_the_map(self):
        self.assertIn("fetch-depth: 0", self.text)
        self.assertIn("restore-keys: carte-", self.text)
        self.assertIn("python3 scripts/test-plan.py plan --out plan.json --map "
                      "carte/carte.json.gz", self.text)
        self.assertIn("--annotations", self.text)

    def test_plan_job_passes_the_pushed_range(self):
        # #833: a push is planned from github.event.before, not HEAD^1 alone ;
        # #2328 : la pointe cible figée dans la description de l'étiquette
        # nomme la composition, même si pull_request.base.sha est périmé.
        self.assertIn("AMEESH_CI_BASE: ${{ github.event_name == 'pull_request' && "
                      "github.event.label.description || (github.event_name == 'push' && "
                      "github.event.before || 'HEAD^1') }}", self.text)
        self.assertIn('--base "$AMEESH_CI_BASE" --ref "$AMEESH_CI_REF"', self.text)

    def test_dynamic_matrices(self):
        for driver in ("psycopg", "psql"):
            self.assertIn(f"part: ${{{{ fromJSON(needs.plan.outputs.{driver}) }}}}", self.text)
            self.assertIn(f"if: needs.plan.outputs.{driver} != '[]'", self.text)
        # L161f review (2): with code changed, an empty psycopg matrix is an
        # error. #3251 : le classifieur de l'agrégat porte la règle (psql sans
        # part = saut légitime, psycopg vide = négatif) ; l'étape lui passe les
        # deux listes de parts et les deux résultats.
        job = self.text[self.text.index("\n  tests:\n"):]
        self.assertIn("--psql-parts '${{ needs.plan.outputs.psql }}'", job)
        self.assertIn('--psycopg "${{ needs.psycopg.result }}"', job)
        self.assertIn('--psql "${{ needs.psql.result }}"', job)
        self.assertNotRegex(job, r'test\s+"\$\{\{\s*needs\.psycopg\.result')

    def test_leaks_job_never_required(self):
        job = self.text[self.text.index("\n  bilan:\n"):self.text.index("\n  tests:\n")]
        self.assertIn("continue-on-error: true", job)
        self.assertIn("test-plan.py leaks plan.json flux/*/flux.jsonl", job)
        self.assertIn("name: leaks", job)
        tests_job = self.text[self.text.index("\n  tests:\n"):]
        self.assertNotIn("bilan", tests_job.split("steps:")[0])

    def test_plan_job_passes_the_branch_context(self):
        # R1 Codex (B1) : le plan qualifie les PR de release sur leurs
        # branches source et cible (« release/** » ou « integration/** ») ;
        # le workflow les lui transmet (L1211). #1237 (N1) : un nom de branche
        # est choisi par l'auteur de la PR ; il passe par l'environnement et
        # reste cité dans le script, jamais interpolé dans un `run:`.
        self.assertIn("BASE_REF: ${{ github.base_ref }}", self.text)
        self.assertIn("HEAD_REF: ${{ github.head_ref }}", self.text)
        self.assertIn('--base-ref "$BASE_REF"', self.text)
        self.assertIn('--head-ref "$HEAD_REF"', self.text)
        for body in _run_bodies(self.text):
            self.assertNotIn("github.base_ref", body)
            self.assertNotIn("github.head_ref", body)
        self.assertEqual([], _branch_ref_escapes(self.text))

    def test_fs_class_lane_follows_the_plan_release_output(self):
        # #748 / #1247 R2 : le job « autre système de fichiers » ne rejoue la
        # suite que pour une release (source ou cible `release/**`) ou à la
        # main ; son `if:` lit l'output `release` du plan — posé par un step
        # qui reçoit les branches par `env:` — et n'interpole aucun nom de
        # branche dans une condition.
        self.assertIn("release: ${{ steps.release.outputs.release }}", self.text)
        fs_class = self.text[self.text.index("\n  fs-class:\n"):
                             self.text.index("\n  platform-macos:\n")]
        self.assertIn("needs.plan.outputs.release == 'true'", fs_class)
        condition = fs_class.split("    if:", 1)[1].split("    runs-on:", 1)[0]
        self.assertNotIn("github.head_ref", condition)
        self.assertNotIn("github.base_ref", condition)
        self.assertIn('case "$BASE_REF" in release/*) release=true ;; esac', self.text)
        self.assertIn('case "$HEAD_REF" in release/*) release=true ;; esac', self.text)

    def test_lot_branch_triggers_and_full_suite_events(self):
        # L1211 : la PR de lot garde la CI (impactés + contrat, le sélecteur
        # décide), main et release/** portent la suite complète ; le check
        # requis reste l'agrégat « tests ». #1410 : une poussée sur la branche
        # d'intégration du paquet ne déclenche plus ce workflow — une tête de
        # paquet n'a qu'une suite, celle de sa PR ; le portier paquet.yml
        # appelle la suite (workflow_call) quand aucune PR ouverte ne couvre
        # la tête exacte. #1343 : la cible de PR garde integration/** (une PR
        # de paquet doit pouvoir demander son ci-full) ; seule la poussée
        # integration/** est hors du déclencheur direct.
        push_start = self.text.index("\n  push:\n") + len("\n  push:\n")
        push = self.text[push_start:self.text.index("\n", push_start)]
        self.assertIn('branches: [main, "release/**"]', push)
        self.assertNotIn("integration", push)
        pull_start = self.text.index("\n  pull_request:\n") + len("\n  pull_request:\n")
        pull = self.text[pull_start:self.text.index("\n  workflow_call:\n")]
        self.assertIn('branches: [main, "release/**", "integration/**"]', pull)
        self.assertIn("types: [labeled]", pull)
        self.assertIn("\n  workflow_call:\n", self.text)
        self.assertIn("L1211", self.text)
        self.assertIn("tests/parts/toujours.txt", self.text)
        self.assertIn("une PR de release", self.text)

    def test_no_lot_push_ci_review_first(self):
        # #1343 (canon §3, home 66) : une poussée sur une PR de LOT ne lance
        # plus rien — la CI part au gel, par l'étiquette `ci-lot-<sha>` /
        # `ci-full-<sha>` posée sur la PR (`pull_request` `types: [labeled]`).
        # Les poussées main/release gardent la suite complète ; celles
        # d'integration/** passent par le portier paquet.yml (§12).
        # Le dispatch manuel reste un point d'entrée d'exploitation : hors PR,
        # il ne peut pas satisfaire un check requis de PR ; la porte de gel,
        # elle, n'existe que sur le parcours PR.
        self.assertIn('  pull_request:\n    types: [labeled]\n    branches: [main, '
                      '"release/**", "integration/**"]\n', self.text)
        self.assertIn('branches: [main, "release/**", "integration/**"]', self.text)
        self.assertIn("\n  workflow_dispatch:\n", self.text)
        # ancré sur l'étape gel : le `if` du job version ne doit pas suffire.
        self.assertIn("        id: gel\n        if: github.event_name == 'pull_request'\n",
                      self.text)

    def test_label_gate_exact_frozen_sha_and_composition(self):
        # #1343 : le job plan ne teste que la composition demandée — étiquette
        # exacte `ci-lot/ci-full-<40hex>`, SHA égal à `pull_request.head.sha`,
        # checkout = merge-ref composé de la base de l'étiquette et de cette tête.
        # Un refus rougit le check requis sans exécuter de tests, jamais un vert
        # sur une autre tête.
        self.assertIn("^ci-(lot|full)-[0-9a-f]{40}$", self.text)
        self.assertIn("foreign label, nothing run", self.text)
        self.assertIn('if [ "$sha" != "$head" ]', self.text)
        self.assertIn("source or target release/** or integration/** requires ci-full-<sha>",
                      self.text)
        self.assertIn("the merge-ref base is", self.text)
        self.assertIn("the merge-ref head is", self.text)
        self.assertIn('--mode "$AMEESH_CI_MODE"', self.text)

    def test_label_concurrency_is_distinct_from_a_foreign_label(self):
        # #1343 : une étiquette étrangère a sa propre concurrency — l'ajouter
        # n'annule pas un run gelé valide.
        self.assertIn("format('pr-{0}-{1}', github.event.pull_request.number, "
                      "github.event.label.name)", self.text)
        self.assertIn("cancel-in-progress: ${{ github.event_name == 'pull_request' }}",
                      self.text)

    def test_label_aggregate_refuses_a_green_without_tests(self):
        # #1343 : une demande de gel (étiquette ci-lot/ci-full) ne prend jamais
        # les voies version seule (L157) ou documentation seule (L161a) ; si le
        # plan en produisait une, l'agrégat requis rougit au lieu de verdir
        # sans tests.
        job = self.text[self.text.index("\n  tests:\n"):]
        # #3251 : la règle vit dans scripts/ci-agregat.py, qui reçoit
        # l'événement et les deux drapeaux ; il refuse ces voies sur une
        # demande de gel (tests/test_3251_agregat_annulation.py) et l'étape
        # n'ajoute aucune expression hors de son contrat.
        self.assertIn('--event "${{ github.event_name }}"', job)
        self.assertIn('--version-only "${{ needs.plan.outputs.version_only }}"', job)
        self.assertIn('--docs-only "${{ needs.plan.outputs.docs_only }}"', job)

    def test_run_name_carries_the_freeze_request(self):
        # #1343 : le titre du run porte la PR, l'étiquette (mode et SHA) et la
        # base, pour que la commande et la preuve de fusion puissent le relire.
        self.assertIn("run-name:", self.text)
        self.assertIn("format(' [PR #{0} {1} base {2}]'", self.text)
        self.assertIn("github.event.label.name", self.text)
        run_name = next(line for line in self.text.splitlines() if line.startswith("run-name:"))
        self.assertIn("github.event.label.description", run_name)
        self.assertNotIn("github.event.pull_request.base.sha", run_name)


class LotFreezePlanTest(unittest.TestCase):
    """#1343 — la CI d'un lot au gel (canon §3) : impactés + contrat sur la
    tête demandée par l'étiquette `ci-lot-<sha>`, jamais une voie qui saute les
    tests ; `ci-full-<sha>` et release/** gardent la suite complète."""

    def setUp(self):
        self.repo = _Repo()
        self.addCleanup(self.repo.close)

    def _card(self) -> dict:
        mods = parts.modules()
        card = self.repo.card()
        card["modules"] = {m: {"durees": {"psycopg": 1.0, "psql": 1.0}, "echecs": []}
                           for m in mods}
        return card

    def _code_change(self) -> None:
        self.repo.write("src/ameesh/code.py", CODE_V1.replace("x + 1", "x + 2"))
        self.repo.commit()

    def _plan(self, mode: str, *, base_ref: str = "main",
              head_ref: str = "lot/1343-ci-au-gel", nature: dict | None = None) -> dict:
        nat = nature or {"version_only": False, "docs_only": False, "why": "", "files": []}
        with mock.patch.object(plan, "nature", lambda *a, **k: dict(nat)):
            return plan.make_plan("pull_request", self._card(), NOW, head="HEAD",
                                  repo=self.repo.path, base="HEAD^1", mode=mode,
                                  base_ref=base_ref, head_ref=head_ref)

    def test_gel_selectionne_impactes_et_contrat(self):
        self._code_change()
        touched = parts.modules()[0]
        card = self._card()
        card["blocs"]["src/ameesh/code.py"]["used"]["modules"] = [touched]
        nat = {"version_only": False, "docs_only": False, "why": "", "files": []}
        with mock.patch.object(plan, "nature", lambda *a, **k: dict(nat)):
            p = plan.make_plan("pull_request", card, NOW, head="HEAD",
                               repo=self.repo.path, base="HEAD^1", mode="lot",
                               base_ref="main", head_ref="lot/x")
        self.assertEqual(p["selection"]["mode"], "impacted")
        # la raison du plan est le gel, pas l'événement (`nature` dit « not a
        # pull request » pour un ancien dispatch) : le plan.json relu le dit.
        self.assertEqual(p["why"], "lot CI at freeze: impacted + contract")
        ran = [m for ps in p["parts"]["psycopg"] for m in ps["modules"]]
        self.assertIn(touched, ran)
        self.assertTrue(p["contract"])
        for module in p["contract"]:
            self.assertIn(module, ran)
        # le pilote psql est borné à sa liste (3 parts au plus, L1211)
        self.assertLessEqual(len(p["parts"]["psql"]), 3)

    def test_gel_ne_prend_jamais_les_voies_qui_sautent_les_tests(self):
        # L157 (version seule) et L161a (documentation seule) restent des voies
        # de poussée : au gel, le contrat tourne, donc un check vert veut dire
        # que des tests ont tourné.
        for why, nat in (("version", {"version_only": True, "docs_only": False,
                                      "why": "version seule",
                                      "files": ["pyproject.toml"]}),
                         ("documentation", {"version_only": False, "docs_only": True,
                                            "why": "documentation seule",
                                            "files": ["docs/x.md"]})):
            with self.subTest(why):
                p = self._plan("lot", nature=nat)
                self.assertFalse(p["version_only"], p["why"])
                self.assertFalse(p["docs_only"], p["why"])
                self.assertEqual(p["doc_readers"], [])
                self.assertNotEqual(p["parts"]["psycopg"], [])

    def test_gel_sur_une_branche_release_garde_la_suite_complete(self):
        self._code_change()
        p = self._plan("lot", head_ref="release/1.10.0")
        self.assertEqual(p["selection"]["mode"], "full")
        self.assertIn("release/**", p["selection"]["why"])
        self.assertEqual([ps["of"] for ps in p["parts"]["psql"]],
                         _full_suite_parts("psql"))
        self.assertFalse(any(ps.get("pilot_list") for ps in p["parts"]["psql"]))

    def test_demande_full_garde_la_suite_complete(self):
        self._code_change()
        p = self._plan("full")
        self.assertEqual(p["selection"]["mode"], "full")
        self.assertEqual([ps["of"] for ps in p["parts"]["psycopg"]],
                         _full_suite_parts("psycopg"))
        self.assertEqual([ps["of"] for ps in p["parts"]["psql"]],
                         _full_suite_parts("psql"))

    def test_demande_full_ne_prend_pas_la_voie_version_seule(self):
        # #1343 : une demande explicite `ci-full-<sha>` ne peut pas verdir par
        # la voie version seule (L157) — la suite complète tourne.
        p = self._plan("full", nature={"version_only": True, "docs_only": False,
                                       "why": "version seule",
                                       "files": ["pyproject.toml"]})
        self.assertFalse(p["version_only"], p["why"])
        self.assertNotEqual(p["parts"]["psycopg"], [])
        self.assertEqual([ps["of"] for ps in p["parts"]["psql"]],
                         _full_suite_parts("psql"))

    def test_repli_d_un_gel_garde_la_forme_du_lot(self):
        p = plan.fallback_plan("pull_request", "boom", mode="lot",
                               base_ref="main", head_ref="lot/x")
        # #3343 : le repli reste la suite complète, même au gel d'un lot ; il
        # prend les parts de main (16 psql, 8 psycopg), jamais les 3 d'un lot.
        self.assertEqual([ps["of"] for ps in p["parts"]["psycopg"]],
                         _full_suite_parts("psycopg"))
        self.assertEqual([ps["of"] for ps in p["parts"]["psql"]],
                         _full_suite_parts("psql"))
        self.assertTrue(all(ps.get("pilot_list") for ps in p["parts"]["psql"]))

    def test_repli_d_un_gel_release_garde_la_suite_complete(self):
        p = plan.fallback_plan("pull_request", "boom", mode="lot",
                               base_ref="main", head_ref="release/1.10.0")
        self.assertEqual([ps["of"] for ps in p["parts"]["psycopg"]],
                         _full_suite_parts("psycopg"))
        self.assertEqual([ps["of"] for ps in p["parts"]["psql"]],
                         _full_suite_parts("psql"))

    def test_cli_mode_lot_sans_base_refuse(self):
        # sans la base demandée, la sélection porterait sur HEAD^1 et
        # manquerait des modules du lot : la CLI refuse plutôt que de deviner.
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = plan.main(["plan", "--out", str(self.repo.path / "plan.json"),
                              "--event", "pull_request", "--mode", "lot"])
        self.assertEqual(code, 2)
        self.assertIn("requires --base", err.getvalue())
        self.assertFalse((self.repo.path / "plan.json").exists())


if __name__ == "__main__":
    unittest.main()
