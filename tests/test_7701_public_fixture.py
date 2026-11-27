# SPDX-License-Identifier: AGPL-3.0-only
"""#7701 (#5902): the public fixture kit, contract revision 1.5.

The kit is ``ameesh.public.fixture``: an installed consumer creates its own
disposable PostgreSQL database, applies the released schema through
``ameesh migrate`` and keeps every file under a private temporary root.  The
witness under ``examples/fixtures/public-fixture-witness`` is a separate
distribution outside ``src/``; it imports only the public Python surfaces and
the standard library, and it runs the installed executables as subprocesses.

These tests freeze the boundary before the database test: the name derivation,
the refusals, the path and view allowlists, the witness imports.  The database
test uses ``AMEESH_TEST_DSN`` when it is set, else the suite bench
(``tests.support.TEST_DSN``, never the ambient ``AMEESH_DSN``: that variable
may name the live mesh database); both name a server whose role may
``CREATE DATABASE``.  A bench whose role lacks ``CREATEDB`` is skipped while
naming the missing right (#7943).  The database test proves that the witness
ran on a fresh ``ameesh_fixture_*`` database and that the database is gone
afterwards.  The deliberate-break test mutates a copy of the surface and
requires the frozen comparator of :mod:`tests.test_7665_module_contract` to
name the break.
"""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from importlib import util
from unittest import mock

from tests import support  # isolate host config before product imports
from tests.test_7665_module_contract import fixture_breaks
from ameesh import dsn as dsn_mod
from ameesh.public import fixture as fixture_mod

ROOT = Path(__file__).resolve().parents[1]
WITNESS = ROOT / "examples" / "fixtures" / "public-fixture-witness"
WITNESS_FILE = WITNESS / "fixture_witness.py"
#: The explicit test DSN when the run carries one, else the suite bench
#: (#7952: the composer's local series have no explicit DSN).  Never the ambient
#: AMEESH_DSN, which may name the live mesh database (#7701 safety decision).
ADMIN = os.environ.get("AMEESH_TEST_DSN") or support.TEST_DSN
DATABASE_NAME = re.compile(r"^ameesh_fixture_[a-z0-9_]{4,64}$")
#: core tables: the fixture hands back one opaque connection, never SQL access.
CORE_TABLES = ("work_items", "agent_mailbox", "agent_registry", "spend_pending",
               "canon_state", "project_modules")
#: the witness may import these standard-library modules and these surfaces.
ALLOWED_IMPORTS = {"json", "os", "sys", "threading", "typing"}
ALLOWED_FROM = {("__future__", "annotations"), ("ameesh.public", "database"),
                ("ameesh.public", "fixture"), ("ameesh.public", "mail"),
                ("typing", "Any"), ("typing", "Mapping")}


def psql(statement: str) -> str:
    done = subprocess.run(["psql", "-w", "-v", "ON_ERROR_STOP=1", "-d", ADMIN,
                           "-Atc", statement],
                          capture_output=True, text=True, timeout=60)
    if done.returncode != 0:
        raise AssertionError("psql failed: %s" % (done.stderr or "").strip()[-800:])
    return done.stdout.strip()


def bench_can_create_database() -> bool:
    """#7943 N1 : le rôle du banc a-t-il CREATEDB (CREATE DATABASE) ?

    Le témoin public crée puis jette une base ; sans ce droit, le test ne peut
    pas le qualifier. On nomme le droit manquant avant de lancer le témoin
    plutôt que de laisser remonter une erreur psql brute."""
    return psql(
        "SELECT bool_or(rolcreatedb OR rolsuper) FROM pg_roles"
        " WHERE pg_has_role(current_user, oid, 'USAGE')") == "t"


class DatabaseNameTest(unittest.TestCase):
    """`dsn.with_database`: one derived name, every other component kept."""

    NAME = "ameesh_fixture_ab12cd34"

    def test_a_uri_keeps_userinfo_authority_query_and_fragment(self):
        self.assertEqual(
            dsn_mod.with_database(
                "postgresql://user:secret@db.example:6543/old?sslmode=require",
                self.NAME),
            "postgresql://user:secret@db.example:6543/%s?sslmode=require" % self.NAME)

    def test_a_uri_without_a_path_gains_one_before_the_query(self):
        self.assertEqual(dsn_mod.with_database("postgresql://user@host:5432", self.NAME),
                         "postgresql://user@host:5432/%s" % self.NAME)
        self.assertEqual(dsn_mod.with_database(
            "postgresql://user@host:5432?sslmode=require", self.NAME),
            "postgresql://user@host:5432/%s?sslmode=require" % self.NAME)
        self.assertEqual(dsn_mod.with_database(
            "postgresql:///old?host=/var/run/postgresql", self.NAME),
            "postgresql:///%s?host=/var/run/postgresql" % self.NAME)

    def test_a_query_dbname_cannot_win_over_the_derived_path(self):
        self.assertEqual(dsn_mod.with_database(
            "postgresql://user@host/old?dbname=other&sslmode=require", self.NAME),
            "postgresql://user@host/%s?sslmode=require" % self.NAME)

    def test_keyword_and_bare_forms_are_replaced_not_duplicated(self):
        self.assertEqual(dsn_mod.with_database(
            "host=db user=u dbname=old", self.NAME),
            "host=db user=u dbname=%s" % self.NAME)
        self.assertEqual(dsn_mod.with_database("old", self.NAME), self.NAME)

    def test_a_non_identifier_name_is_refused(self):
        for name in ("", "1abc", "a-b", "a b", 'x"; DROP DATABASE postgres'):
            with self.subTest(name):
                with self.assertRaises(dsn_mod.DsnError):
                    dsn_mod.with_database("postgresql://u@h/old", name)


class FixtureBoundaryTest(unittest.TestCase):
    """The fixture refuses an unsafe target before any connection exists."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="w7701-boundary-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.dsn = "postgresql://user:secret@127.0.0.1:1/old"

    def rooted(self, **extra):
        return fixture_mod.Fixture(self.dsn, root=os.path.join(self.tmp, "root"), **extra)

    def test_the_admin_dsn_is_required_and_never_shown(self):
        for value in (None, "", "   "):
            with self.subTest(value):
                with self.assertRaises(fixture_mod.FixtureRefused):
                    fixture_mod.Fixture(value)
        self.assertNotIn("secret", repr(fixture_mod.Fixture(self.dsn)))

    def test_the_prefix_stays_in_the_published_namespace(self):
        for prefix in ("fixture_", "public_", "ameesh_fixture_bad-name"):
            with self.subTest(prefix):
                with self.assertRaises(fixture_mod.FixtureRefused):
                    fixture_mod.Fixture(self.dsn, prefix=prefix)
        self.assertEqual(fixture_mod.Fixture(
            self.dsn, prefix="ameesh_fixture_").prefix, "ameesh_fixture_")

    def test_a_command_prefix_is_never_split_or_imported(self):
        self.assertEqual(fixture_mod.Fixture(self.dsn, executable="ameesh").executable,
                         ("ameesh",))
        self.assertEqual(fixture_mod.Fixture(
            self.dsn, executable=["ameesh", "run"]).executable, ("ameesh", "run"))

    def test_an_uncreated_fixture_refuses_its_published_accessors(self):
        fixture = fixture_mod.Fixture(self.dsn)
        for call in (lambda: fixture.dsn, lambda: fixture.root, fixture.env,
                     fixture.connect, lambda: fixture.run(["true"]),
                     lambda: fixture.read_view("agent_mesh_overview", ["name"])):
            with self.subTest(call):
                with self.assertRaises(fixture_mod.FixtureRefused):
                    call()
        fixture.close()
        fixture.close()  # idempotent, even when nothing was created

    def test_only_a_name_this_fixture_created_can_be_dropped(self):
        fixture = self.rooted()
        fixture._database = "postgres"
        with self.assertRaises(fixture_mod.FixtureRefused):
            fixture._drop()
        other = fixture_mod.Fixture(self.dsn, prefix="ameesh_fixture_a")
        other._database = "ameesh_fixture_bcde"
        with self.assertRaises(fixture_mod.FixtureRefused):
            other._drop()

    def test_the_prefix_keeps_the_full_name_inside_the_postgres_limit(self):
        # PostgreSQL truncates identifiers at 63 bytes; the random suffix is
        # 16 bytes, so a 47-byte prefix is the longest one that keeps the
        # name the fixture later validates and drops.
        longest = "ameesh_fixture_" + "a" * (fixture_mod._MAX_DB_NAME - 16
                                             - len("ameesh_fixture_"))
        self.assertEqual(len(longest), 47)
        fixture_mod.Fixture(self.dsn, prefix=longest)
        with self.assertRaises(fixture_mod.FixtureRefused):
            fixture_mod.Fixture(self.dsn, prefix=longest + "a")
        bounded = fixture_mod.Fixture(self.dsn, prefix=longest)
        bounded._database = longest + "b" * 16
        self.assertEqual(len(bounded._database), 63)
        bounded._database = longest + "b" * 17  # 64 bytes: never emitted
        with self.assertRaises(fixture_mod.FixtureRefused):
            bounded._drop()

    def test_a_failed_migration_drops_the_database_it_created(self):
        fixture = self.rooted()
        statements = []
        fixture._admin = statements.append
        fixture._migrate = lambda: (_ for _ in ()).throw(
            fixture_mod.FixtureError("migration failed"))
        with self.assertRaises(fixture_mod.FixtureError):
            fixture.create()
        self.assertEqual(len(statements), 2)
        self.assertIn("CREATE DATABASE", statements[0])
        self.assertIn("DROP DATABASE", statements[1])
        self.assertFalse(fixture._created)
        self.assertLessEqual(len(fixture._database), 63)

    def test_close_finishes_cleanup_when_the_drop_fails(self):
        fixture = fixture_mod.Fixture(self.dsn)
        root = tempfile.mkdtemp(prefix="w7701-owned-")
        fixture._root = root
        fixture._owns_root = True
        fixture._created = True
        fixture._database = "ameesh_fixture_abcdefgh"
        fixture._drop = lambda: (_ for _ in ()).throw(
            fixture_mod.FixtureError("drop failed"))
        with self.assertRaises(fixture_mod.FixtureError):
            fixture.close()
        self.assertTrue(fixture._closed)
        self.assertFalse(os.path.exists(root))
        fixture.close()  # idempotent: the failure is not replayed

    def test_the_psql_fallback_keeps_the_password_out_of_argv(self):
        fixture = fixture_mod.Fixture(
            "postgresql://user:secret@db.example:5432/old")
        seen = {}
        done = subprocess.CompletedProcess(args=[], returncode=0, stdout="",
                                           stderr="")

        def fake_run(argv, **kwargs):
            seen["argv"], seen["env"] = argv, kwargs.get("env")
            return done

        with mock.patch("subprocess.run", fake_run):
            fixture._admin_psql('CREATE DATABASE "ameesh_fixture_abcdefgh"')
        self.assertNotIn("secret", " ".join(seen["argv"]))
        self.assertEqual(seen["env"]["PGPASSWORD"], "secret")
        self.assertIn("postgresql://user@db.example:5432/old", seen["argv"])

    def test_a_path_cannot_leave_the_private_root(self):
        fixture = self.rooted()
        for relative in ("", "..", "../outside", "/etc/passwd", "a/../../b"):
            with self.subTest(relative):
                with self.assertRaises(fixture_mod.FixtureRefused):
                    fixture.path(relative)
        fixture.write("notes/one.txt", "hello")
        self.assertEqual(fixture.read("notes/one.txt"), "hello")
        with self.assertRaises(fixture_mod.FixtureRefused):
            fixture.write("two.txt", 2)

    def test_the_named_views_are_an_explicit_allowlist(self):
        fixture = self.rooted()
        for view, columns in (("work_items", ["id"]),
                              ("agent_mesh_overview", []),
                              ("agent_mesh_overview", ["*"]),
                              ("agent_mesh_overview", ["name; DROP TABLE work_items"])):
            with self.subTest((view, columns)):
                with self.assertRaises(fixture_mod.FixtureRefused):
                    fixture.read_view(view, columns)
        # An admitted view still needs a created database: no SQL escape hatch
        # is opened just because the name is admitted.
        with self.assertRaises(fixture_mod.FixtureRefused):
            fixture.read_view("agent_mesh_overview", ["name"])

    def test_a_canon_needs_files(self):
        with self.assertRaises(fixture_mod.FixtureRefused):
            self.rooted().canon({})


class WitnessBoundaryTest(unittest.TestCase):
    """The witness is an installed consumer, not a test of the package."""

    def source(self):
        return WITNESS_FILE.read_text(encoding="utf-8")

    def test_only_the_standard_library_and_ameesh_public_are_imported(self):
        tree = ast.parse(self.source(), str(WITNESS_FILE))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertIn(alias.name, ALLOWED_IMPORTS, alias.name)
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0, node)
                for alias in node.names:
                    self.assertIn((node.module, alias.name), ALLOWED_FROM,
                                  (node.module, alias.name))
            elif isinstance(node, ast.Call) and getattr(node.func, "id", "") in (
                    "__import__", "import_module"):
                self.fail("dynamic import in %s" % WITNESS_FILE.name)

    def test_the_witness_reaches_no_core_table_or_sql_escape_hatch(self):
        source = self.source()
        for table in CORE_TABLES:
            with self.subTest(table):
                self.assertNotRegex(source, r"\b%s\b" % table)
        self.assertNotRegex(source, r"\.(execute|query|cursor)\s*\(")
        for driver in ("psycopg", "sqlite3", "subprocess"):
            with self.subTest(driver):
                self.assertNotRegex(source, r"\b(import|from)\s+%s\b" % driver)
        self.assertTrue((WITNESS / "README.md").is_file())
        self.assertNotIn(ROOT / "src", WITNESS.parents)
        self.assertNotIn(ROOT / "tests", WITNESS.parents)


@unittest.skipUnless(support.REQUIRE_DB, "banc Postgres requis (AMEESH_REQUIRE_DB=0)")
class FixtureDatabaseTest(unittest.TestCase):
    """The external witness qualifies through the published fixture only."""

    maxDiff = None

    def drop_if_left(self, database):
        if DATABASE_NAME.match(database or ""):
            subprocess.run(["psql", "-w", "-d", ADMIN, "-c",
                            'DROP DATABASE IF EXISTS "%s" WITH (FORCE)' % database],
                           capture_output=True, text=True, timeout=60)

    def witness_env(self):
        env = dict(os.environ)
        # The witness runs the public Python surface in this process: a
        # verified runner identity would refuse an actor named by the witness,
        # so the consumer process is clean.  The child executables are isolated
        # by fixture.env() regardless of what the parent carries.
        for name in ("AGENT_MAIL_NAME", "AMEESH_RUNNER_ID"):
            env.pop(name, None)
        # Pollution an installed consumer may carry: none of it may reach a
        # child executable, which must use the disposable database and root.
        env.update({
            "AMEESH_FIXTURE_ADMIN_DSN": ADMIN,
            "AMEESH_DSN": ADMIN,
            "AMEESH_CANON": "/nonexistent/canon",
        })
        parts = [str(ROOT / "src"), str(ROOT), str(WITNESS)]
        parts += [part for part in os.environ.get("PYTHONPATH", "").split(os.pathsep) if part]
        env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(parts))
        return env

    def test_the_external_witness_qualifies_through_the_public_fixture(self):
        if not bench_can_create_database():
            self.skipTest(
                "bench role has no CREATEDB: ameesh.public.fixture needs a role "
                "allowed to CREATE DATABASE")
        admin_database = psql("SELECT current_database()")
        done = subprocess.run([sys.executable, str(WITNESS_FILE)], env=self.witness_env(),
                              cwd=str(WITNESS), capture_output=True, text=True,
                              timeout=900)
        try:
            report = json.loads(done.stdout)
        except ValueError:
            self.fail("witness printed no JSON (exit %d): %s%s"
                      % (done.returncode, (done.stdout or "")[-1000:],
                         (done.stderr or "")[-1000:]))
        database = (report.get("fixture") or {}).get("database", "")
        if database:
            self.addCleanup(self.drop_if_left, database)

        self.assertEqual(done.returncode, 0, json.dumps(report, indent=1)[-3000:])
        self.assertTrue(report.get("ok"), report.get("error"))
        self.assertEqual(report["schema"], "ameesh-public-fixture-witness/1")
        self.assertEqual(report["checks"], ["fixture", "canon", "process", "usage",
                                            "mail", "connect", "views", "teardown"])
        self.assertTrue(DATABASE_NAME.match(database), database)
        self.assertNotEqual(database, admin_database)
        self.assertEqual(report["teardown"], {"database": database, "closed": True})
        # The witness drops its database before printing: the disposable name
        # is gone from the consumer's server, not merely closed.
        self.assertEqual(psql("SELECT count(*) FROM pg_database WHERE datname = '%s'"
                              % database), "0")
        self.assertRegex(report["canon"]["commit"], r"^[0-9a-f]{40}$")
        self.assertEqual(report["process"]["stage"], "review")
        self.assertTrue(report["process"]["admitted"])
        self.assertTrue(report["process"]["refused"])
        self.assertEqual(set(report["mail"]["refusals"]),
                         {"reserved_actor", "unknown_recipient", "idempotency_conflict"})
        self.assertEqual(report["mail"]["refusals"]["reserved_actor"]["code"], "refused")
        self.assertEqual(report["mail"]["refusals"]["unknown_recipient"]["code"], "refused")
        self.assertEqual(report["mail"]["refusals"]["idempotency_conflict"]["code"],
                         "conflict")
        self.assertTrue({"orchestre", "relecteur"} <= set(report["views"]["agents"]))

        identical = report["mail"]["identical"]
        self.assertEqual(len(identical), 2)
        self.assertEqual(len({row["id"] for row in identical if "id" in row}), 1)
        self.assertEqual(sum(1 for row in identical if row.get("reused")), 1)
        conflicting = report["mail"]["conflicting"]
        self.assertEqual(len(conflicting), 2)
        self.assertEqual(sum(1 for row in conflicting if "id" in row), 1)
        self.assertEqual(sum(1 for row in conflicting
                             if row.get("code") == "conflict"), 1)

        # The production connection of contract 1.6: a clean child process
        # connects with no argument, uses public mail, closes, and observes the
        # three documented refusals without exposing the DSN.
        connect = report["connect"]
        self.assertEqual(connect["package_contract"], "1.6")
        self.assertEqual(connect["mail_contract"], "1.4")
        self.assertIn("public connect hello", connect["bodies"])
        self.assertFalse(connect["repr_shows_dsn"])
        self.assertEqual({name: outcome["code"] for name, outcome
                          in connect["refusals"].items()},
                         {"schema": "schema", "storage": "unsupported",
                          "unavailable": "unavailable"})
        for name, outcome in connect["refusals"].items():
            with self.subTest(name):
                self.assertFalse(outcome["shows_dsn"], outcome)


class DeliberateBreakTest(unittest.TestCase):
    """A mutated fixture surface is named by the frozen comparator."""

    def load(self, source):
        directory = tempfile.mkdtemp(prefix="w7701-break-")
        self.addCleanup(shutil.rmtree, directory, True)
        path = Path(directory) / "fixture.py"
        path.write_text(source, encoding="utf-8")
        suffix = re.sub(r"[^a-z0-9]", "", os.path.basename(directory).lower())
        name = "ameesh.public._fixture_break_%s" % suffix
        spec = util.spec_from_file_location(name, path)
        module = util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(name, None)
        return module

    def mutated(self, old, new):
        source = (ROOT / "src" / "ameesh" / "public" / "fixture.py").read_text(
            encoding="utf-8")
        self.assertEqual(source.count(old), 1, old)
        return self.load(source.replace(old, new))

    def test_a_removed_public_parameter_is_detected(self):
        module = self.mutated("def env(self, **extra: str) -> dict:",
                              "def env(self) -> dict:")
        breaks = fixture_breaks(module)
        self.assertTrue(any(reason.startswith("env:") and "extra" in reason
                            for reason in breaks), breaks)

    def test_a_changed_published_constant_is_detected(self):
        module = self.mutated('DATABASE_PREFIX = "ameesh_fixture_"',
                              'DATABASE_PREFIX = "fixture_"')
        breaks = fixture_breaks(module)
        self.assertTrue(any(reason.startswith("DATABASE_PREFIX:") for reason in breaks),
                        breaks)

    def test_a_removed_error_code_is_detected(self):
        module = self.mutated('class FixtureRefused(FixtureError):\n    """The fixture '
                              'refuses an unsafe target or an out-of-protocol call."""\n\n'
                              '    code = "refused"',
                              'class FixtureRefused(FixtureError):\n    """The fixture '
                              'refuses an unsafe target or an out-of-protocol call."""\n\n'
                              '    code = "error"')
        breaks = fixture_breaks(module)
        self.assertTrue(any(reason.startswith("FixtureRefused:") for reason in breaks),
                        breaks)


if __name__ == "__main__":
    unittest.main()
