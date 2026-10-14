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
import datetime
import importlib.util
import io
import json
import pathlib
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

    def test_changed_lines_without_a_test(self):
        self.repo.write("src/ameesh/code.py", CODE_V1.replace("x * 2", "x * 3"))
        self.repo.commit()
        impact = self.repo.judge()
        self.assertEqual(impact.p1, {})
        self.assertEqual(impact.untested, [{"file": "src/ameesh/code.py", "block": "untested",
                                            "lines": [10, 10]}])

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
        self.assertEqual(impact.p1, {"test_a": "src/ameesh/code.py::used"})
        self.assertEqual(impact.unknown_blocks, ["src/ameesh/code.py::helper"])

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
        def judged(line: str) -> dict:
            self.repo.code = CODE_V1 + line + "\n"  # the map knows the line
            self.repo.write("src/ameesh/code.py", self.repo.code)
            self.repo.base = self.repo.commit()
            self.repo.write("src/ameesh/code.py", self.repo.code + "\n\ndef _extra():\n    pass\n")
            self.repo.commit()
            return self.repo.judge().p1

        for why, line in {"getattr with a computed name": "f = getattr(Box, '_' + 'x', None)",
                          "globals()": "g = globals()",
                          "import *": "from os.path import *"}.items():
            with self.subTest(why):
                self.assertIn("test_c", judged(line), why)
        # a literal getattr name stays static: the new function is taken out
        self.assertEqual(judged("f = getattr(Box, 'size')"), {})

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

    def test_phase2_unsafe_with_changed_lines_without_a_test(self):
        # L161f review (4)
        self.repo.write("src/ameesh/code.py", CODE_V1.replace("x * 2", "x * 3"))
        self.repo.commit()
        out = self._classify()
        self.assertTrue(out["sure"])
        self.assertEqual([u["block"] for u in out["untested"]], ["untested"])
        self.assertFalse(out["phase2_safe"])
        self.repo.write("src/ameesh/code.py", CODE_V1.replace("x + 1", "x + 2"))
        self.repo.commit()
        self.assertTrue(self._classify()["phase2_safe"])

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
                    self.assertEqual([ps["of"] for ps in p["parts"]["psycopg"]], [4] * 4)
                    self.assertEqual([ps["of"] for ps in p["parts"]["psql"]], [6] * 6)
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
        self.assertEqual([ps["of"] for ps in release["parts"]["psql"]], [6] * 6)

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
        old = NOW + datetime.timedelta(hours=49)
        self.assertIn("older than 48 h", plan.map_problem(card, old, "HEAD", self.repo.path)[0])
        stranger = dict(card, commit="0" * 40)
        self.assertIn("not an ancestor", plan.map_problem(stranger, NOW, "HEAD",
                                                          self.repo.path)[0])

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
        self.assertEqual(always, ["test_339_orphan_settle", "test_cluster",
                                  "test_events_attach", "test_l31_orphans",
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

    def _plan(self, files: list[str], event: str = "pull_request") -> dict:
        docs = {"version_only": False, "docs_only": event == "pull_request",
                "why": "docs", "files": files}
        with mock.patch.object(plan, "nature", lambda *a, **k: dict(docs)), \
                mock.patch.object(plan.parts, "modules", lambda *a: list(self.MODULES)), \
                mock.patch.object(plan, "read_list", lambda *a, **k: list(self.MODULES)
                                  if a and a[0] == plan.PILOT_FILE else []):
            return plan.make_plan(event, self.card, NOW, head="HEAD", repo=self.repo.path,
                                  base_ref="main", head_ref="lot/x")

    def _base_with_unmeasured_ci_change(self):
        self.card["genere"] = (NOW - datetime.timedelta(hours=6.8)).isoformat()
        self.repo.write(".github/workflows/tests.yml", "name: tests\n")
        self.repo.write("scripts/test-plan.py", "# CI changed on main after the map\n")
        return self.repo.commit()

    def _real_plan(self, base):
        with mock.patch.object(plan.parts, "modules", lambda *a: list(self.MODULES)), \
                mock.patch.object(plan, "read_list", lambda *a, **k: list(self.MODULES)
                                  if a and a[0] == plan.PILOT_FILE else []):
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
        self.assertEqual(sorted(m for part in p["parts"]["psycopg"]
                                for m in part["modules"]), self.MODULES)

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

    def test_stale_map_always_runs_the_full_suite(self):
        # codex (1): a 49 h map left only the declared readers
        self.card["donnees"]["docs/guide.md"] = [self.name["test_db"]]
        self.card["genere"] = (NOW - datetime.timedelta(hours=49)).isoformat()
        self.repo.write("docs/guide.md", "# Guide, again\n")
        self.repo.commit()
        self.assertFullSuite(self._plan(["docs/guide.md"]), "map not usable")
        self.assertFullSuite(self._plan_without_map(["docs/guide.md"]), "no map")

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
        self.assertEqual(sorted(m for ps in p["parts"]["psql"] for m in ps["modules"]), mods)
        self.assertEqual([ps["of"] for ps in p["parts"]["psql"]], [6] * 6)
        out = plan.outputs(p)
        self.assertEqual(json.loads(out["psycopg"]), [1, 2, 3, 4])
        self.assertEqual(out["psql_parts"], "6")
        self.assertEqual(out["code"], "true")

    def test_pull_request_runs_the_pilot_modules_with_psql(self):
        p = plan.make_plan("pull_request", None, NOW, base_ref="main", head_ref="lot/x")
        if p["version_only"] or p["docs_only"]:
            self.skipTest("the checked out commit is a version or documentation change")
        pilot = parts.read_list(plan.PILOT_FILE, parts.modules())
        psql = p["parts"]["psql"]
        # #947 : trois parts (PARTS), les modules pilotes une fois chacun
        self.assertEqual([ps["part"] for ps in psql], [1, 2, 3])
        self.assertEqual([ps["of"] for ps in psql], [3, 3, 3])
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
        self.assertEqual(plan.PARTS["psql"][1], 6)  # suite complète sur push : inchangée

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

    def test_fallback_plan_lets_the_parts_split(self):
        p = plan.fallback_plan("pull_request", "boom")
        f = pathlib.Path(tempfile.mkdtemp()) / "plan.json"
        self.addCleanup(shutil.rmtree, f.parent)
        f.write_text(json.dumps(p), encoding="utf-8")
        self.assertIsNone(parts.read_plan(f, "psql", 1))
        self.assertTrue(parts._driver_plan(str(f)))
        self.assertEqual(json.loads(plan.outputs(p)["psycopg"]), [1, 2, 3, 4])
        # #947 : le plan de repli garde le découpage de la part psql d'une PR
        self.assertEqual(json.loads(plan.outputs(p)["psql"]), [1, 2, 3])
        self.assertEqual([ps["of"] for ps in p["parts"]["psql"]], [3, 3, 3])

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
        # #749 : une fusion a main passe la suite complete sous les deux pilotes
        # (psql en 6 parts), pas seulement la CI de release.
        self.assertEqual([ps["of"] for ps in p["parts"]["psql"]], [6] * 6)
        for driver in ("psycopg", "psql"):
            with self.subTest(driver=driver):
                seen = [m for ps in p["parts"][driver] for m in ps["modules"]]
                self.assertEqual(sorted(seen), mods)  # every one, none twice

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
        self.assertIn("psycopg=[1, 2, 3, 4]", (d / "out").read_text(encoding="utf-8"))


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
                    "used": True, "problem": ""},
            "sure": True, "phase2_safe": True, "phase2_why": "", "unsure": [],
            "untested": [], "unknown_blocks": [], "foundation": [], "changed": 2,
            "always": [], "contract": list(self.contract),
            "classes": {m: list(c) for m, c in classes.items()},
        }
        state.update(kw)
        return state

    def _plan(self, event="pull_request", classes=None, card=None,
              base_ref="main", head_ref="lot/1211-ci-impactes-contrat",
              nature_kw=None, **state_kw):
        state = self._state(classes if classes is not None else self._classes(), **state_kw)
        nature = {"version_only": False, "docs_only": False, "why": "", "files": []}
        nature.update(nature_kw or {})
        with mock.patch.object(plan, "classify", return_value=state), \
                mock.patch.object(plan, "nature", return_value=dict(nature)):
            return plan.make_plan(event, card if card is not None else {}, NOW,
                                  base_ref=base_ref, head_ref=head_ref)

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
            "unsafe selection": {"phase2_safe": False,
                                 "phase2_why": "3 changed block(s) without a test"},
            "empty contract": {"contract": []},
        }
        for why, state_kw in cases.items():
            with self.subTest(why):
                p = self._plan(**state_kw)
                self.assertEqual(p["selection"]["mode"], "full", why)
                self.assertTrue(p["selection"]["why"], why)
                self.assertEqual(self._ran(p), sorted(self.mods), why)

    def test_full_suite_on_main_release_and_integration(self):
        for event in ("push", "workflow_dispatch"):
            with self.subTest(event):
                p = self._plan(event=event)
                self.assertEqual(p["selection"]["mode"], "full")
                self.assertIn("integration", p["selection"]["why"])
                self.assertEqual(self._ran(p), sorted(self.mods))

    def test_release_pull_request_never_takes_the_impacted_selection(self):
        # R1 Codex (B1) : la carte est fraîche et sûre (les impactés sont
        # décidables), mais une PR dont la source ou la cible est release/**
        # ou integration/** garde la suite complète, les deux pilotes, sans
        # liste pilote psql (canon §3).
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
                self.assertEqual(self._ran(p, "psql"), sorted(self.mods), why)
                self.assertEqual([ps["of"] for ps in p["parts"]["psycopg"]], [4] * 4)
                self.assertEqual([ps["of"] for ps in p["parts"]["psql"]], [6] * 6)
                self.assertFalse(any(ps.get("pilot_list") for ps in p["parts"]["psql"]))
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

    def test_fallback_of_a_release_pull_request_keeps_both_pilots_full(self):
        # R1 Codex : « y compris fallback » — le repli d'une PR de release ne
        # reprend ni la liste pilote psql ni le découpage d'une PR de lot.
        p = plan.fallback_plan("pull_request", "boom", base_ref="main",
                               head_ref="release/1.10.0")
        self.assertEqual(json.loads(plan.outputs(p)["psycopg"]), [1, 2, 3, 4])
        self.assertEqual(json.loads(plan.outputs(p)["psql"]), [1, 2, 3, 4, 5, 6])
        self.assertFalse(any(ps.get("pilot_list") for ps in p["parts"]["psql"]))
        self.assertIn("release/**", p["selection"]["why"])
        lot = plan.fallback_plan("pull_request", "boom", base_ref="main", head_ref="lot/x")
        self.assertEqual(json.loads(plan.outputs(lot)["psql"]), [1, 2, 3])
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
        full = self._plan(phase2_safe=False, phase2_why="changed block(s) without a test")
        text = plan.summary(full)
        self.assertIn("full suite", text)
        self.assertIn("changed block(s) without a test", text)

    def test_parts_shrink_to_the_estimate(self):
        def card(seconds):
            return {"modules": {m: {"durees": {"psycopg": seconds, "psql": seconds}}
                                for m in self.mods}}
        self.assertEqual(plan.lot_part_count("psycopg", self.mods[:10], card(1.0)), 1)
        # 235 modules × 2 s = 470 s -> deux parts
        self.assertEqual(plan.lot_part_count("psycopg", self.mods, card(2.0)), 2)
        self.assertGreaterEqual(plan.lot_part_count("psycopg", self.mods, card(5.0)), 4)

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


def _run_bodies(text: str) -> list[str]:
    """Le texte de chaque bloc `run:` de tests.yml (inline ou littéral)."""
    bodies: list[str] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        indent = len(line) - len(line.lstrip(" "))
        if not stripped.startswith("run:"):
            i += 1
            continue
        inline = stripped[len("run:"):].strip()
        if inline and inline not in ("|", ">", "|-", ">-", "|+", ">+"):
            bodies.append(inline)
            i += 1
            continue
        body, j = [], i + 1
        while j < len(lines):
            nxt = lines[j]
            if nxt.strip() and len(nxt) - len(nxt.lstrip(" ")) <= indent:
                break
            body.append(nxt)
            j += 1
        bodies.append("\n".join(body))
        i = j
    return bodies


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
        # #833: a push is planned from github.event.before, not HEAD^1 alone
        self.assertIn("AMEESH_CI_BASE: ${{ github.event_name == 'push' && "
                      "github.event.before || 'HEAD^1' }}", self.text)
        self.assertIn('--base "$AMEESH_CI_BASE" --ref "$AMEESH_CI_REF"', self.text)

    def test_dynamic_matrices(self):
        for driver in ("psycopg", "psql"):
            self.assertIn(f"part: ${{{{ fromJSON(needs.plan.outputs.{driver}) }}}}", self.text)
            self.assertIn(f"if: needs.plan.outputs.{driver} != '[]'", self.text)
        # L161f review (2): with code changed, an empty psycopg matrix is an error
        skipped = ("test '${{{{ needs.plan.outputs.{0} }}}}' = '[]' && test "
                   "\"${{{{ needs.{0}.result }}}}\" = skipped")
        job = self.text[self.text.index("\n  tests:\n"):]
        self.assertIn(skipped.format("psql"), job)
        self.assertNotIn(skipped.format("psycopg"), job)
        self.assertIn('test "${{ needs.psycopg.result }}" = success\n', job)

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

    def test_lot_branch_triggers_and_full_suite_events(self):
        # L1211 : la PR de lot garde la CI (impactés + contrat, le sélecteur
        # décide), le push vers la branche d'intégration du paquet, main et
        # release/** porte la suite complète ; le check requis reste
        # l'agrégat « tests ».
        self.assertIn('branches: [main, "release/**", "integration/**"]', self.text)
        self.assertIn("\n  pull_request:\n", self.text)
        self.assertIn("L1211", self.text)
        self.assertIn("tests/parts/toujours.txt", self.text)
        self.assertIn("une PR de release", self.text)


if __name__ == "__main__":
    unittest.main()
