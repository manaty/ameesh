# SPDX-License-Identifier: AGPL-3.0-only
"""L161f — test selection from the measured map.

`scripts/test-plan.py plan` classes every test module once per CI run (P0
changed test, P1 impacted by changed blocks, files, directories or tables,
P2 recent failure, P3 the rest) and writes the parts; every part runs from
that plan (`scripts/test-parts.py lancer --plan`) and says when its impacted
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

REPO = pathlib.Path(__file__).resolve().parent.parent


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


plan = _load("test_plan_l161f", "test-plan.py")
parts = _load("test_parts_l161f", "test-parts.py")
sys.path.insert(0, str(REPO / "scripts"))
import carte  # noqa: E402

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
        infos, _ = carte.blocs(CODE_V1)
        users = {"<module>": ["test_a", "test_b", "test_c"], "used": ["test_a"],
                 "untested": [], "Box": ["test_b"], "Box.get": ["test_b"]}
        return {
            "schema": carte.SCHEMA, "commit": self.base, "genere": NOW.isoformat(),
            "modules": {m: {"durees": {"psycopg": 1.0}, "echecs": []}
                        for m in ("test_a", "test_b", "test_c", "test_db", "test_old")},
            "blocs": {"src/ameesh/code.py": {n: {**i, "modules": users[n]}
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

    def test_new_method_overriding_a_known_name_counts_for_its_class(self):
        added = "\n    def put(self):\n        pass\n"
        self.repo.write("src/ameesh/code.py", CODE_V1 + added)
        self.repo.commit()
        self.assertNotIn("test_b", self.repo.judge().p1)  # `put` exists nowhere
        self.repo.write("src/ameesh/code.py", CODE_V1 + added.replace("put", "get"))
        self.repo.commit()  # a second `get`: may override, Box changed
        self.assertIn("test_b", self.repo.judge().p1)

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
        card = {"modules": {
            "test_recent": {"echecs": [{"ts": "2026-10-09T00:00:00+00:00"}]},
            "test_old": {"echecs": [{"ts": "2026-09-30T00:00:00+00:00"}]}}}
        self.assertEqual(list(plan.recent_failures(card, NOW)), ["test_recent"])

    def test_always_list_sorted_and_known(self):
        always = plan.read_list(plan.ALWAYS_FILE, parts.modules())
        self.assertEqual(always, ["test_cluster", "test_events_attach", "test_l72_panne_de_base"])

    def test_new_functions_taken_out_losslessly(self):
        # taking nothing out must give the very fingerprints of the map
        for f in sorted((REPO / "src").rglob("*.py")):
            source = f.read_text(encoding="utf-8")
            def prints(blocks):
                return {k: v["empreinte"] for k, v in blocks.items()}
            self.assertEqual(prints(plan._without_new_functions(source, set(), set())),
                             prints(carte.blocs(source)[0]), f)


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
        p = plan.make_plan("pull_request", None, NOW)
        if p["version_only"] or p["docs_only"]:
            self.skipTest("the checked out commit is a version or documentation change")
        pilot = parts.lire_liste(plan.PILOT_FILE, parts.modules())
        self.assertEqual([ps["modules"] and sorted(ps["modules"]) for ps in p["parts"]["psql"]],
                         [pilot])

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
        self.assertIsNone(parts.lire_plan(f, "psql", 1))
        self.assertTrue(parts._plan_pilote(str(f)))
        self.assertEqual(json.loads(plan.outputs(p)["psycopg"]), [1, 2, 3, 4])

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
        flux = parts.Flux(d / "flux.jsonl", {"part": "psycopg 1/1"})
        cls = type("_R", (parts._Resultat,), {"flux": flux, "classes": {},
                                              "impactes": impacted})
        suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(c)
                                   for c in cases)
        unittest.TextTestRunner(stream=io.StringIO(), resultclass=cls).run(suite)
        flux.fermer()
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


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.text = (REPO / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")

    def test_plan_job_reads_the_map(self):
        self.assertIn("fetch-depth: 0", self.text)
        self.assertIn("restore-keys: carte-", self.text)
        self.assertIn("python3 scripts/test-plan.py plan --out plan.json --map "
                      "carte/carte.json.gz", self.text)
        self.assertIn("--annotations", self.text)

    def test_dynamic_matrices(self):
        for driver in ("psycopg", "psql"):
            self.assertIn(f"part: ${{{{ fromJSON(needs.plan.outputs.{driver}) }}}}", self.text)
            self.assertIn(f"if: needs.plan.outputs.{driver} != '[]'", self.text)
            self.assertIn(f"test '${{{{ needs.plan.outputs.{driver} }}}}' = '[]' && test "
                          f"\"${{{{ needs.{driver}.result }}}}\" = skipped", self.text)

    def test_leaks_job_never_required(self):
        job = self.text[self.text.index("\n  bilan:\n"):self.text.index("\n  tests:\n")]
        self.assertIn("continue-on-error: true", job)
        self.assertIn("test-plan.py leaks plan.json flux/*/flux.jsonl", job)
        self.assertIn("name: leaks", job)
        tests_job = self.text[self.text.index("\n  tests:\n"):]
        self.assertNotIn("bilan", tests_job.split("steps:")[0])


if __name__ == "__main__":
    unittest.main()
