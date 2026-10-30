# SPDX-License-Identifier: AGPL-3.0-only
"""L158 — a version number matches its content (scripts/version-level.py).

The CI job `version number` runs it on every PR; the auditor runs `release`
on each published version. Throwaway repositories: a tag, then commits that
add a migration, a command, an option, a setting, a destructive migration or
a breaking trailer, and the number the PR declares.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("version_level_l158",
                                               ROOT / "scripts" / "version-level.py")
vl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vl)

CLI = '''def build(sub):
    p = sub.add_parser("show")
    p.add_argument("--json", action="store_true")
'''


class VersionLevelTest(unittest.TestCase):
    def setUp(self):
        self.repo = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.repo)
        self.addCleanup(setattr, vl, "REPO", None)
        vl.REPO = str(self.repo)
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.email", "t@example.com")
        self._git("config", "user.name", "t")
        self._write("src/ameesh/migrations/0001_init.sql", "create table a (x int);\n")
        self._write("src/ameesh/cli.py", CLI)
        self._commit("1.2.3", "init")
        self._git("tag", "v1.2.3")

    def _git(self, *args):
        subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True)

    def _write(self, name, text):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def _commit(self, version, message):
        self._write("pyproject.toml", '[project]\nname = "ameesh"\nversion = "%s"\n' % version)
        self._git("add", "-A")
        self._git("commit", "-q", "-m", message)

    def _pr(self):
        """The release PR: its version commit, checked against main's tag."""
        return self._main(["pr", "HEAD^1", "HEAD"])

    def _main(self, argv):
        with contextlib.redirect_stdout(io.StringIO()):
            return vl.main(argv)

    def test_bug_fix_is_a_patch(self):
        self._write("src/ameesh/cli.py", CLI + "# fixed\n")
        self._commit("1.2.3", "fix")
        self.assertEqual(vl.level("v1.2.3", "HEAD"), ("patch", ["package code changed"]))
        self._commit("1.2.4", "ameesh 1.2.4")
        self.assertEqual(self._pr(), 0)

    def test_new_migration_needs_a_minor(self):
        self._write("src/ameesh/migrations/0002_b.sql",
                    "-- drop table a; in a comment\nalter table a drop constraint c;\n"
                    "create table b (y int);\n")
        self._commit("1.2.3", "feature")
        floor, reasons = vl.level("v1.2.3", "HEAD")
        self.assertEqual((floor, reasons), ("minor", ["migration 0002_b.sql added"]))
        self._commit("1.2.4", "ameesh 1.2.4")
        self.assertEqual(self._pr(), 1)
        self._commit("1.3.0", "ameesh 1.3.0")
        self.assertEqual(self._pr(), 0)

    def test_new_command_option_or_setting_needs_a_minor(self):
        self._write("src/ameesh/cli.py", CLI + '''    p.add_argument("-a", "--all")
    q = sub.add_parser("land")
    TTL = os.environ.get("AMEESH_LAND_TTL")
''')
        self._commit("1.2.3", "feature")
        floor, reasons = vl.level("v1.2.3", "HEAD")
        self.assertEqual(floor, "minor")
        self.assertEqual(reasons, ["command land added", "option show --all added",
                                   "option show -a added", "setting AMEESH_LAND_TTL added"])

    def test_breaking_changes_need_a_major(self):
        cases = {
            "destructive migration": lambda: self._write(
                "src/ameesh/migrations/0002_b.sql", "ALTER TABLE a\n  DROP COLUMN x;\n"),
            "changed migration": lambda: self._write(
                "src/ameesh/migrations/0001_init.sql", "create table a (x bigint);\n"),
            "option removed": lambda: self._write(
                "src/ameesh/cli.py", CLI.replace('    p.add_argument("--json", '
                                                 'action="store_true")\n', "")),
        }
        for name, change in cases.items():
            with self.subTest(name):
                self._git("checkout", "-q", "-B", "case", "v1.2.3")
                change()
                self._commit("1.2.3", "change")
                self.assertEqual(vl.level("v1.2.3", "HEAD")[0], "major")
                self._commit("1.3.0", "ameesh 1.3.0")
                self.assertEqual(self._pr(), 1)
                self._commit("2.0.0", "ameesh 2.0.0")
                self.assertEqual(self._pr(), 0)

    def _floor(self, cli, change, base="v1.2.3"):
        """The floor of `cli` (tagged) → `change`."""
        self._git("checkout", "-q", "-B", "case", base)
        self._write("src/ameesh/cli.py", cli)
        self._commit("1.2.3", "cli")
        self._git("tag", "-f", "case-base")
        self._write("src/ameesh/cli.py", change)
        self._commit("1.2.3", "change")
        return vl.level("case-base", "HEAD")

    def test_every_option_string_counts_not_its_order(self):
        cli = ('def build(sub):\n    p = sub.add_parser("run")\n'
               '    p.add_argument("--debut", "--start", "-s")\n')
        floor, reasons = self._floor(cli, cli.replace(', "--start"', ""))
        self.assertEqual((floor, reasons[0]), ("major", "option run --start removed"))
        self.assertEqual(self._floor(cli, cli.replace('"--debut", "--start"',
                                                      '"--start", "--debut"'))[0], "patch")

    def test_command_alias_removed_is_a_break(self):
        cli = 'def build(sub):\n    p = sub.add_parser("show", aliases=["view"])\n'
        floor, reasons = self._floor(cli, cli.replace(', aliases=["view"]', ""))
        self.assertEqual((floor, reasons[0]), ("major", "command view removed"))

    def test_options_belong_to_their_command(self):
        cli = ('def _json(p):\n    p.add_argument("--json", action="store_true")\n\n\n'
               'def build(sub):\n    work = sub.add_parser("work").add_subparsers()\n'
               '    p = work.add_parser("show")\n    _json(p)\n'
               '    g = p.add_mutually_exclusive_group()\n    g.add_argument("--all")\n'
               '    p = work.add_parser("list")\n    _json(p)\n')
        self.assertEqual(self._floor(cli, cli.replace("    _json(p)\n    g", "    g", 1)),
                         ("major", ["option work show --json removed"]))
        self.assertEqual(self._floor(cli, cli.replace('    g.add_argument("--all")\n', ""))[1],
                         ["option work show --all removed"])
        # a command removed is reported once, not with each of its options
        self.assertEqual(self._floor(cli, cli.replace('work.add_parser("show")',
                                                      'work.add_parser("view")'))[1],
                         ["command work show removed"])

    def test_setting_removed_is_a_break(self):
        cli = CLI + '    TTL = os.environ.get("AMEESH_LAND_TTL")\n'
        self.assertEqual(self._floor(cli, CLI),
                         ("major", ["setting AMEESH_LAND_TTL removed"]))

    def test_destructive_sql(self):
        major = ("ALTER TABLE a DROP x;", 'ALTER TABLE a ALTER COLUMN "x" TYPE integer;',
                 "alter table a alter x set data type bigint;",
                 "alter table a drop if exists x;", "drop sequence s;", "DROP SCHEMA s CASCADE;",
                 "alter table a alter column x set not null;", "alter table a rename to b;",
                 "truncate a;", "delete from a where x = 1;",
                 "create temp table t (x int) on commit drop;\ndrop table a;")
        minor = ("drop view if exists v;\ncreate view v as select 1;",
                 "drop trigger if exists t on a;", "drop index if exists i;",
                 "alter table a drop constraint if exists c;",
                 "alter table a alter column x set default 0;",
                 "alter table a alter column x drop not null;",
                 "drop function if exists f();", "alter type t add value 'v';",
                 # 0460 (#2470): a temporary table dropped at commit holds no data
                 "create temp table t on commit drop as select 1 as x;\n"
                 "drop view if exists v;\ncreate view v as select x from t;",
                 "CREATE TEMPORARY TABLE t (x int) ON  COMMIT\n  DROP;")
        for sql, expected in [(s, "major") for s in major] + [(s, "minor") for s in minor]:
            with self.subTest(sql):
                self._git("checkout", "-q", "-B", "case", "v1.2.3")
                self._write("src/ameesh/migrations/0002_b.sql", sql + "\n")
                self._commit("1.2.3", "migration")
                self.assertEqual(vl.level("v1.2.3", "HEAD")[0], expected)

    def test_runs_from_any_directory(self):
        """The auditor runs it from the checkout's parent: git still runs in
        the checkout that holds the script."""
        script = self.repo / "scripts" / "version-level.py"
        script.parent.mkdir()
        shutil.copy(ROOT / "scripts" / "version-level.py", script)
        proc = subprocess.run(["python3", str(script.relative_to(self.repo.parent)),
                               "level", "v1.2.3", "HEAD"], cwd=self.repo.parent,
                              capture_output=True, text=True)
        self.assertEqual((proc.returncode, proc.stderr), (0, ""))
        self.assertTrue(proc.stdout.startswith("patch"), proc.stdout)

    def test_separately_routed_parsers_keep_their_options(self):
        cli = ('import argparse\n\n\ndef run_main(argv):\n'
               '    parser = argparse.ArgumentParser(prog="ameesh run")\n'
               '    parser.add_argument("--wait")\n\n\n'
               'def attach_main(argv):\n'
               '    parser = argparse.ArgumentParser(prog="ameesh attach")\n'
               '    parser.add_argument("--wait")\n\n\n'
               'def other_main(argv):\n'
               '    parser = argparse.ArgumentParser()\n'
               '    parser.add_argument("--wait")\n')
        floor, reasons = self._floor(cli, cli.replace('    parser.add_argument("--wait")\n', "", 1))
        self.assertEqual((floor, reasons), ("major", ["option run --wait removed"]))
        floor, reasons = self._floor(cli, cli[:cli.rindex('    parser.add_argument')])
        self.assertEqual((floor, reasons), ("major", ["option cli:other_main --wait removed"]))

    def test_project_parser_class_reads_like_argparse(self):
        """L148 : un parseur bâti par `HiddenAliasParser(prog=…)` garde ses
        commandes et ses options lisibles (sinon le niveau vire à `major`)."""
        cli = ('def plan_main(argv):\n'
               '    parser = HiddenAliasParser(prog="ameesh plan")\n'
               '    sub = parser.add_subparsers(dest="plan_command", required=True)\n'
               '    p_add = sub.add_parser("add")\n'
               '    p_add.add_argument("--due")\n')
        floor, reasons = self._floor(cli, cli.replace('    p_add.add_argument("--due")\n', "", 1))
        self.assertEqual((floor, reasons), ("major", ["option plan add --due removed"]))

    def test_inlining_a_helper_changes_nothing(self):
        helper = ('def _options(p):\n    p.add_argument("--custom")\n\n\n'
                  'def build(sub):\n    p = sub.add_parser("show")\n    _options(p)\n')
        inlined = 'def build(sub):\n    p = sub.add_parser("show")\n    p.add_argument("--custom")\n'
        self.assertEqual(self._floor(helper, inlined)[0], "patch")
        self.assertEqual(self._floor(inlined, helper)[0], "patch")

    def test_sql_literals_are_not_statements(self):
        for sql in ("CREATE TABLE notes (message text DEFAULT 'rename');",
                    "insert into notes values ('delete from a'), ('it''s; drop table a');",
                    'create table b ("rename" int);',
                    "create function f() returns void as $$ delete from a $$ language sql;",
                    "create function g() returns void as $body$ truncate a; $body$ language sql;"):
            with self.subTest(sql):
                self._git("checkout", "-q", "-B", "case", "v1.2.3")
                self._write("src/ameesh/migrations/0002_b.sql", sql + "\n")
                self._commit("1.2.3", "migration")
                self.assertEqual(vl.level("v1.2.3", "HEAD")[0], "minor")
        self._write("src/ameesh/migrations/0002_b.sql",
                    "insert into notes values ('x');\nalter table \"a\" rename to b;\n")
        self._commit("1.2.3", "migration")
        self.assertEqual(vl.level("v1.2.3", "HEAD")[0], "major")

    def _migration_level(self, sql):
        self._git("checkout", "-q", "-B", "case", "v1.2.3")
        self._write("src/ameesh/migrations/0002_b.sql", sql + "\n")
        self._commit("1.2.3", "migration")
        return vl.level("v1.2.3", "HEAD")[0]

    def test_sql_bodies_and_escapes(self):
        # r3/r4: a DO block runs at migration time; a dollar body closes on
        # its own tag only; E'' strings take backslash escapes
        cases = (("DO $$ BEGIN ALTER TABLE a DROP COLUMN x; END $$;", "major"),
                 ("do language plpgsql $x$ begin execute 'alter table a drop column y'; "
                  "end $x$;", "major"),
                 ("DO $$ BEGIN RAISE NOTICE 'rename later'; END $$;", "minor"),
                 ("SELECT $body$ safe $$; DELETE FROM a; $body$;", "minor"),
                 ("INSERT INTO notes VALUES (E'it\\'s rename');", "minor"),
                 ("INSERT INTO notes VALUES (E'a\\\\'); DELETE FROM a;", "major"),
                 ("/* nested /* drop table a; */ still a comment */ create table c (y int);",
                  "minor"),
                 # r5: a body in a plain string, a quoted language, a dollar
                 # string run by EXECUTE
                 ("DO 'BEGIN ALTER TABLE a DROP COLUMN x; END';", "major"),
                 ("DO 'BEGIN RAISE NOTICE ''rename later''; END';", "minor"),
                 ('DO LANGUAGE "plpgsql" $$ BEGIN DELETE FROM a; END $$;', "major"),
                 ("DO $do$ BEGIN EXECUTE $$DELETE FROM a$$; END $do$;", "major"),
                 ("DO $do$ BEGIN RAISE NOTICE '%', $$DELETE FROM a$$; END $do$;", "minor"))
        for sql, expected in cases:
            with self.subTest(sql):
                self.assertEqual(self._migration_level(sql), expected)

    def test_helper_called_by_name(self):
        positional = ('def _options(p):\n    p.add_argument("--custom")\n\n\n'
                      'def build(sub):\n    p = sub.add_parser("show")\n    _options(p)\n')
        named = positional.replace("_options(p)\n", "_options(p=p)\n")
        self.assertEqual(self._floor(positional, named)[0], "patch")
        floor, reasons = self._floor(named, named.replace('"--custom"', '"--other"'))
        self.assertEqual((floor, reasons[0]), ("major", "option show --custom removed"))

    def test_parsers_filled_in_another_module(self):
        # r3/r4: `plan_cli.add_parsers(work_sub, …)` fills `work`, not the top
        self._git("checkout", "-q", "-B", "case", "v1.2.3")
        self._write("src/ameesh/plan_cli.py",
                    'def add_parsers(work_sub, func):\n'
                    '    p = work_sub.add_parser("link")\n    p.add_argument("--none")\n')
        cli = ('def build(sub):\n    from . import plan_cli\n'
               '    work_sub = sub.add_parser("work").add_subparsers()\n'
               '    plan_cli.add_parsers(work_sub, None)\n')
        self._write("src/ameesh/cli.py", cli)
        self._commit("1.2.3", "cli")
        self._git("tag", "-f", "case-base")
        self.assertIn("work link --none", vl.surface("case-base")["option"])
        self.assertNotIn("link --none", vl.surface("case-base")["option"])
        self._write("src/ameesh/cli.py", cli.replace("add_parsers(work_sub,", "add_parsers(sub,"))
        self._commit("1.2.3", "moved")
        floor, reasons = vl.level("case-base", "HEAD")
        self.assertEqual(floor, "major")
        self.assertIn("command work link removed", reasons)
        # imported by name, the same
        self._write("src/ameesh/cli.py", cli.replace("from . import plan_cli", "from .plan_cli "
                                                     "import add_parsers").replace(
            "plan_cli.add_parsers", "add_parsers"))
        self._commit("1.2.3", "by name")
        self.assertEqual(vl.level("case-base", "HEAD")[0], "patch")

    def test_a_command_moved_to_a_module_through_a_deprecated_alias_stays_present(self):
        # #4719: `ameesh.merge_pr` is only `sys.modules[__name__] = _moved`
        # (#4382); the parser its callers register is the real module's, so
        # `work merge-pr` and its options are not a removal.
        self._git("checkout", "-q", "-B", "case", "v1.2.3")
        self._write("src/ameesh/cli.py",
                    'def build(sub):\n    work = sub.add_parser("work")\n'
                    '    p = work.add_parser("merge-pr")\n'
                    '    p.add_argument("--head", required=True)\n')
        self._commit("1.2.3", "before the move")
        self._git("tag", "-f", "moved-base")
        self._write("src/ameesh/modules/dev/__init__.py", "")
        self._write("src/ameesh/modules/dev/merge_pr.py",
                    'def add_parser(work, func):\n'
                    '    p = work.add_parser("merge-pr", aliases=["mp"])\n'
                    '    p.add_argument("--head", required=True)\n')
        self._write("src/ameesh/merge_pr.py",
                    'import sys\n\nfrom .modules.dev import merge_pr as _merge_pr\n\n'
                    'sys.modules[__name__] = _merge_pr\n')
        self._write("src/ameesh/cli.py",
                    'def build(sub):\n    from . import merge_pr\n'
                    '    work = sub.add_parser("work")\n'
                    '    merge_pr.add_parser(work, None)\n')
        self._commit("1.2.3", "moved")
        surface = vl.surface("HEAD")
        self.assertIn("work merge-pr", surface["command"])
        self.assertIn("work mp", surface["command"])
        self.assertIn("work merge-pr --head", surface["option"])
        floor, reasons = vl.level("moved-base", "HEAD")
        self.assertEqual(floor, "minor")  # the new alias; the move adds no break
        self.assertNotIn("command work merge-pr removed", reasons)

    def test_a_command_dropped_from_the_module_it_moved_to_is_a_break(self):
        # the deprecated alias must not hide a real removal (#4719)
        self._git("checkout", "-q", "-B", "case", "v1.2.3")
        self._write("src/ameesh/cli.py",
                    'def build(sub):\n    work = sub.add_parser("work")\n'
                    '    p = work.add_parser("merge-pr")\n'
                    '    p.add_argument("--head", required=True)\n')
        self._commit("1.2.3", "before the move")
        self._git("tag", "-f", "dropped-base")
        self._write("src/ameesh/modules/dev/__init__.py", "")
        self._write("src/ameesh/modules/dev/merge_pr.py",
                    'def add_parser(work, func):\n'
                    '    p = work.add_parser("other")\n    p.add_argument("--head")\n')
        self._write("src/ameesh/merge_pr.py",
                    'import sys\n\nfrom .modules.dev import merge_pr as _merge_pr\n\n'
                    'sys.modules[__name__] = _merge_pr\n')
        self._write("src/ameesh/cli.py",
                    'def build(sub):\n    from . import merge_pr\n'
                    '    work = sub.add_parser("work")\n'
                    '    merge_pr.add_parser(work, None)\n')
        self._commit("1.2.3", "moved and dropped")
        floor, reasons = vl.level("dropped-base", "HEAD")
        self.assertEqual(floor, "major")
        self.assertIn("command work merge-pr removed", reasons)

    def test_modules_reached_by_dotted_name_reexport_or_local_import(self):
        # r5: `import ameesh.plan_cli` then its full name; a helper a module
        # imports and hands on; an import inside a function binds only there
        self._git("checkout", "-q", "-B", "case", "v1.2.3")
        self._write("src/ameesh/plan_cli.py",
                    'def add_parsers(work_sub, func):\n'
                    '    p = work_sub.add_parser("link")\n    p.add_argument("--none")\n')
        self._write("src/ameesh/queue_cli.py",
                    'def add_parsers(queue_sub, func):\n'
                    '    p = queue_sub.add_parser("drain")\n    p.add_argument("--now")\n')
        self._write("src/ameesh/bridge.py", "from .plan_cli import add_parsers as fill\n")
        cases = {
            "dotted": ('import ameesh.plan_cli\n\n\ndef build(sub):\n'
                       '    work_sub = sub.add_parser("work").add_subparsers()\n'
                       '    ameesh.plan_cli.add_parsers(work_sub, None)\n'),
            "re-exported": ('from . import bridge\n\n\ndef build(sub):\n'
                            '    work_sub = sub.add_parser("work").add_subparsers()\n'
                            '    bridge.fill(work_sub, None)\n'),
            "local": ('def build(sub):\n    from . import plan_cli as helpers\n'
                      '    work_sub = sub.add_parser("work").add_subparsers()\n'
                      '    helpers.add_parsers(work_sub, None)\n\n\n'
                      'def other(sub):\n    from . import queue_cli as helpers\n'
                      '    queue_sub = sub.add_parser("queue").add_subparsers()\n'
                      '    helpers.add_parsers(queue_sub, None)\n'),
        }
        for name, cli in cases.items():
            with self.subTest(name):
                self._write("src/ameesh/cli.py", cli)
                self._commit("1.2.3", name)
                options = vl.surface("HEAD")["option"]
                self.assertIn("work link --none", options)
                self.assertNotIn("link --none", options)
        self.assertIn("queue drain --now", options)
        self.assertNotIn("queue link --none", options)
        self.assertNotIn("work drain --now", options)

    def test_nested_scopes(self):
        # codex #195 r6: a nested function's import does not rebind its
        # enclosing builder's name; a function's own import hides a module
        # function of the same name
        self._git("checkout", "-q", "-B", "case", "v1.2.3")
        self._write("src/ameesh/plan_cli.py",
                    'def fill(work):\n    work.add_parser("link")\n')
        self._write("src/ameesh/other_cli.py", 'def fill(work):\n    pass\n')
        self._commit("1.2.3", "helpers")
        nested = ('from . import plan_cli as helper\n\n\ndef build(sub):\n'
                  '    def unused():\n        from . import other_cli as helper\n'
                  '    work = sub.add_parser("work").add_subparsers()\n'
                  '    helper.fill(work)\n')
        hidden = ('def fill(sub):\n    pass\n\n\ndef build(sub):\n'
                  '    from .plan_cli import fill\n'
                  '    work = sub.add_parser("work").add_subparsers()\n    fill(work)\n')
        # codex #195 r7: a class body is a scope of its own, its imports
        # apply to its calls
        in_class = ('import argparse\n\n\nclass Cli:\n'
                    '    from . import plan_cli as helper\n'
                    '    sub = argparse.ArgumentParser(prog="ameesh").add_subparsers()\n'
                    '    work = sub.add_parser("work").add_subparsers()\n'
                    '    helper.fill(work)\n')
        class_hidden = ('import argparse\n\n\ndef fill(sub):\n    pass\n\n\nclass Cli:\n'
                        '    from .plan_cli import fill\n'
                        '    sub = argparse.ArgumentParser(prog="ameesh").add_subparsers()\n'
                        '    work = sub.add_parser("work").add_subparsers()\n    fill(work)\n')
        # codex #195 r8: an inner class does not see its enclosing class's
        # imports, it starts again from the module's
        nested_class = ('import argparse\nfrom . import plan_cli as helper\n\n\nclass Outer:\n'
                        '    from . import other_cli as helper\n\n'
                        '    class Inner:\n'
                        '        sub = argparse.ArgumentParser(prog="ameesh").add_subparsers()\n'
                        '        work = sub.add_parser("work").add_subparsers()\n'
                        '        helper.fill(work)\n')
        for name, cli in (("nested", nested), ("hidden", hidden), ("class", in_class),
                          ("class-hidden", class_hidden), ("nested-class", nested_class)):
            with self.subTest(name):
                self._git("checkout", "-q", "-B", "case-" + name, "case")
                self._write("src/ameesh/cli.py", cli)
                self._commit("1.2.3", name)
                self._git("tag", "-f", "base-" + name)
                self.assertIn("work link", vl.surface("HEAD")["command"])
                self._write("src/ameesh/cli.py", cli.replace("fill(work)", "fill(sub)"))
                self._commit("1.2.3", "moved")
                floor, reasons = vl.level("base-" + name, "HEAD")
                self.assertEqual(floor, "major")
                self.assertIn("command work link removed", reasons)

    def test_loop_over_literals(self):
        cli = ('def build(sub):\n    for action in ("approve", "retry"):\n'
               '        p = sub.add_parser(action)\n        p.add_argument("--by")\n'
               '    for name, flag in (("show", "--json"), ("list", "--all")):\n'
               '        sub.add_parser(name).add_argument(flag)\n')
        floor, reasons = self._floor(cli, cli.replace('("approve", "retry")', '("retry",)'))
        self.assertEqual((floor, reasons), ("major", ["command approve removed"]))
        floor, reasons = self._floor(cli, cli.replace('("list", "--all")', '("list", "--a")'))
        self.assertEqual((floor, reasons[0]), ("major", "option list --all removed"))

    def test_breaking_trailer(self):
        self._write("src/ameesh/cli.py", CLI + "# mail payload v2\n")
        self._commit("1.2.3", "mail payload v2\n\nameesh-breaking: payload key renamed")
        floor, reasons = vl.level("v1.2.3", "HEAD")
        self.assertEqual(floor, "major")
        self.assertIn("ameesh-breaking: payload key renamed", reasons[0])

    def test_number_must_be_the_next_one(self):
        old = (1, 9, 0)
        for new, level in (((1, 9, 1), "patch"), ((1, 10, 0), "minor"), ((2, 0, 0), "major"),
                           ((1, 11, 0), None), ((1, 10, 1), None), ((1, 8, 9), None)):
            self.assertEqual(vl.bump_level(old, new), level, new)
        ok, message = vl.verdict(old, (1, 11, 0), "patch", [])
        self.assertFalse(ok)
        self.assertIn("1.9.1, 1.10.0 or 2.0.0", message)

    def test_above_the_floor_passes_with_a_note(self):
        ok, message = vl.verdict((1, 9, 0), (1, 10, 0), "patch", ["package code changed"])
        self.assertTrue(ok)
        self.assertIn("above the patch found", message)

    def test_not_a_version_pr(self):
        self._write("src/ameesh/migrations/0002_b.sql", "create table b (y int);\n")
        self._commit("1.2.3", "feature")
        self.assertEqual(self._pr(), 0)

    def test_release_checks_against_the_previous_tag(self):
        for version, code in (("1.2.4", 1), ("1.3.0", 0)):
            with self.subTest(version):
                self._git("checkout", "-q", "-B", "release", "v1.2.3")
                self._write("src/ameesh/migrations/0002_b.sql", "create table b (y int);\n")
                self._commit(version, "ameesh " + version)
                self._git("tag", "v" + version)
                self.assertEqual(self._main(["release", "v" + version]), code)


class WorkflowTest(unittest.TestCase):
    def test_version_job_gates_the_required_check(self):
        text = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
        self.assertIn("python3 scripts/version-level.py pr HEAD^1 HEAD", text)
        # #3251 : la garde de version vit dans scripts/ci-agregat.py, appelé
        # par l'étape « bilan des parts » ; le job `version` reste une
        # dépendance du check requis, comme toutes les parts lues par le bilan.
        self.assertIn("needs: [plan, version, psycopg, psql, platform-macos, "
                      "doc-readers, fs-class]", text)
        self.assertIn('--version "${{ needs.version.result }}"', text)
        # La version est lue avant les voies version seule et doc seule : le
        # classifieur la vérifie avant ses sorties anticipées
        # (tests/test_3251_agregat_annulation.py, témoin dédié).
        self.assertLess(text.index('--version "${{ needs.version.result }}"'),
                        text.index("--version-only"))


if __name__ == "__main__":
    unittest.main()
