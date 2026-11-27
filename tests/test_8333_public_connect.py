# SPDX-License-Identifier: AGPL-3.0-only
"""#8333 (#6573): public production connection, contract revision 1.6.

An installed module opens the deployment's database connection through
``ameesh.public.connect()`` (module :mod:`ameesh.public.database`) instead of
importing ``ameesh.config``, ``ameesh.db`` and ``require_schema()``.  The
surface takes no DSN, checks the schema immediately and returns the opaque core
connection used by :mod:`ameesh.public.mail`.

These tests freeze the public refusals without a database, then qualify the
real path on a consumer-owned disposable database from
:mod:`ameesh.public.fixture`: one in-process connection and one clean
subprocess that imports only ``ameesh.public.*``.  The external witness
``examples/fixtures/public-fixture-witness`` carries the same subprocess proof
for an installed consumer.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests import support  # isolate host config before product imports
from tests.test_7665_module_contract import database_breaks
from ameesh import config as config_mod
from ameesh import db as db_mod
from ameesh.public import database as database_mod
from ameesh.public import fixture as fixture_mod
from ameesh.public import mail as public_mail

ROOT = Path(__file__).resolve().parents[1]
HOST = "atelier"
ACTOR = "orchestre"
PEER = "relecteur"
SCHEMA_ADMIN = os.environ.get("AMEESH_TEST_DSN") or support.TEST_DSN

INDEX = """---
type: Index
title: connect canon
---

# connect canon
"""
FEDERATION = """id: connect
root: canon
members: []
extensions:
  ameesh:
    projects:
      connect:
        modules: [work-types]
"""
MEMBER = """---
type: Member
title: owner
description: "The test human."
roles: [project-lead]
authenticators: []
---

# owner
"""
HOST_CARD = """---
type: Host
title: %s
responsible: human:owner
policy:
  harnesses: [deepseek]
  providers: [deepseek]
  credential_modes: [api-key]
  workspaces:
    roots: {connect: /srv/connect}
  max_agents: 4
---

# %s
""" % (HOST, HOST)


def _agent(name: str) -> str:
    return """---
type: Agent
title: %s
responsible: human:owner
capabilities: [read, propose]
harness: deepseek
provider: deepseek
credential_mode: api-key
---

# %s
""" % (name, name)


def _placement(name: str) -> str:
    return """---
type: Placement
title: %s@%s
agent: %s
hosts: [%s]
credential_mode: api-key
---

# %s on %s
""" % (name, HOST, name, HOST, name, HOST)


def canon_files() -> dict:
    """A minimal consumer canon: one host and the two mail peers."""
    return {
        "index.md": INDEX,
        "federation.yaml": FEDERATION,
        "membres/owner.md": MEMBER,
        "hotes/%s.md" % HOST: HOST_CARD,
        "agents/%s.md" % ACTOR: _agent(ACTOR),
        "agents/%s.md" % PEER: _agent(PEER),
        "placements/%s-%s.md" % (ACTOR, HOST): _placement(ACTOR),
        "placements/%s-%s.md" % (PEER, HOST): _placement(PEER),
    }


#: A clean child of the fixture environment: only ``ameesh.public.*`` imports.
CHILD_WITNESS = r"""
import json
import os

from ameesh.public import database, mail

connection = database.connect()
try:
    receipt = mail.submit(
        connection, os.environ["CONNECT_ACTOR"],
        mail.Submission(os.environ["CONNECT_PEER"], "child connect hello"))
    page = mail.history(connection, os.environ["CONNECT_ACTOR"],
                        peer=os.environ["CONNECT_PEER"])
    print(json.dumps({
        "ok": True,
        "contract": database.CONTRACT_VERSION,
        "mail_contract": mail.CONTRACT_VERSION,
        "id": receipt.id,
        "bodies": [entry.body for entry in page.entries],
        "repr": repr(connection),
    }, sort_keys=True))
finally:
    connection.close()
"""


def bench_can_create_database() -> bool:
    """The fixture witness needs a role allowed to CREATE DATABASE (#7943)."""
    done = subprocess.run(
        ["psql", "-w", "-v", "ON_ERROR_STOP=1", "-d", SCHEMA_ADMIN,
         "-Atc", "SELECT bool_or(rolcreatedb OR rolsuper) FROM pg_roles"
                 " WHERE pg_has_role(current_user, oid, 'USAGE')"],
        capture_output=True, text=True, timeout=60)
    if done.returncode != 0:
        raise AssertionError("psql failed: %s" % (done.stderr or "").strip()[-800:])
    return done.stdout.strip() == "t"


class PublicConnectBoundaryTest(unittest.TestCase):
    """The documented refusals, the immediate schema check and the redaction."""

    @staticmethod
    def config(**kwargs):
        return config_mod.Config(**kwargs)

    def test_the_surface_is_exported_from_the_package(self):
        from ameesh import public

        self.assertIs(public.connect, database_mod.connect)
        self.assertIs(public.database, database_mod)
        self.assertIn("connect", public.__all__)
        self.assertIn("database", public.__all__)

    def test_connect_reads_the_configuration_and_checks_the_schema_immediately(self):
        cfg = self.config()
        connection = mock.Mock()
        with mock.patch.object(database_mod.config_mod, "load",
                               return_value=cfg) as loaded, \
                mock.patch.object(database_mod.db_mod, "connect",
                                  return_value=connection) as opened, \
                mock.patch.object(database_mod.db_mod, "require_schema") as schema:
            result = database_mod.connect()
        self.assertIs(result, connection)
        loaded.assert_called_once_with()
        opened.assert_called_once_with(cfg)
        # No defer=True: the caller never receives an unchecked connection.
        schema.assert_called_once_with(connection)

    def test_a_mediated_backend_is_refused_before_any_connection(self):
        cfg = self.config(backend="mediated", exec_url="http://127.0.0.1:9")
        with mock.patch.object(database_mod.config_mod, "load", return_value=cfg), \
                mock.patch.object(database_mod.db_mod, "connect") as opened:
            with self.assertRaises(database_mod.UnsupportedStorage) as caught:
                database_mod.connect()
        opened.assert_not_called()
        self.assertEqual(caught.exception.code, "unsupported")
        self.assertIsInstance(caught.exception, NotImplementedError)

    def test_a_remote_connection_is_refused_and_closed(self):
        connection = mock.Mock()
        with mock.patch.object(database_mod.config_mod, "load",
                               return_value=self.config()), \
                mock.patch.object(database_mod.db_mod, "connect",
                                  return_value=connection), \
                mock.patch.object(database_mod.storage, "is_remote",
                                  return_value=True):
            with self.assertRaises(database_mod.UnsupportedStorage) as caught:
                database_mod.connect()
        self.assertEqual(caught.exception.code, "unsupported")
        connection.close.assert_called_once_with()

    def test_a_missing_schema_is_refused_and_closed(self):
        connection = mock.Mock()
        with mock.patch.object(database_mod.config_mod, "load",
                               return_value=self.config()), \
                mock.patch.object(database_mod.db_mod, "connect",
                                  return_value=connection), \
                mock.patch.object(database_mod.db_mod, "require_schema",
                                  side_effect=db_mod.SchemaMissing(
                                      "agent-mesh schema missing (database "
                                      "postgresql://agent:s3cr3t@db/x)")):
            with self.assertRaises(database_mod.SchemaRefused) as caught:
                database_mod.connect()
        self.assertEqual(caught.exception.code, "schema")
        self.assertNotIn("s3cr3t", str(caught.exception))
        connection.close.assert_called_once_with()

    def test_a_schema_probe_failure_is_a_schema_refusal(self):
        connection = mock.Mock()
        with mock.patch.object(database_mod.config_mod, "load",
                               return_value=self.config()), \
                mock.patch.object(database_mod.db_mod, "connect",
                                  return_value=connection), \
                mock.patch.object(database_mod.db_mod, "require_schema",
                                  side_effect=db_mod.DbError("permission denied")):
            with self.assertRaises(database_mod.SchemaRefused) as caught:
                database_mod.connect()
        self.assertEqual(caught.exception.code, "schema")
        connection.close.assert_called_once_with()

    def test_a_schema_probe_cut_is_unavailable(self):
        connection = mock.Mock()
        with mock.patch.object(database_mod.config_mod, "load",
                               return_value=self.config()), \
                mock.patch.object(database_mod.db_mod, "connect",
                                  return_value=connection), \
                mock.patch.object(database_mod.db_mod, "require_schema",
                                  side_effect=db_mod.Unavailable("psql: timeout")):
            with self.assertRaises(database_mod.DatabaseUnavailable) as caught:
                database_mod.connect()
        self.assertEqual(caught.exception.code, "unavailable")
        connection.close.assert_called_once_with()

    def test_an_incompatible_schema_from_admission_is_a_schema_refusal(self):
        with mock.patch.object(database_mod.config_mod, "load",
                               return_value=self.config()), \
                mock.patch.object(database_mod.db_mod, "connect",
                                  side_effect=db_mod.DbError("incompatible schema")):
            with self.assertRaises(database_mod.SchemaRefused) as caught:
                database_mod.connect()
        self.assertEqual(caught.exception.code, "schema")

    def test_the_refusal_never_carries_the_dsn_or_the_password(self):
        secret = "s3cr3t-Pa55"
        dsn = "postgresql://agent:%s@db.example:5432/mesh" % secret
        cfg = self.config(dsn=dsn)
        error = db_mod.Unavailable(
            "psycopg: connection failed for %s (password=%s)" % (dsn, secret))
        with mock.patch.object(database_mod.config_mod, "load", return_value=cfg), \
                mock.patch.object(database_mod.db_mod, "connect", side_effect=error):
            with self.assertRaises(database_mod.DatabaseUnavailable) as caught:
                database_mod.connect()
        message = str(caught.exception)
        self.assertEqual(caught.exception.code, "unavailable")
        self.assertNotIn(secret, message)
        self.assertNotIn(dsn, message)
        self.assertNotIn("postgresql://agent:%s" % secret, message)
        self.assertIn("***", message)

    def test_a_libpq_escaped_password_is_redacted(self):
        # Reader report (#235087): the pair regex used to stop at the escaped
        # quote and leave the tail of the password in the public message.
        dsn = "password='first\\'TAIL_SECRET' host=localhost dbname=mesh"
        cfg = self.config(dsn=dsn)
        with mock.patch.object(database_mod.config_mod, "load", return_value=cfg), \
                mock.patch.object(database_mod.db_mod, "connect",
                                  side_effect=db_mod.Unavailable(
                                      "connection rejected: %s" % dsn)):
            with self.assertRaises(database_mod.DatabaseUnavailable) as caught:
                database_mod.connect()
        message = str(caught.exception)
        self.assertNotIn("TAIL_SECRET", message)
        self.assertNotIn("first", message)
        self.assertIn("password=***", message)
        self.assertIn("localhost", message)

    def test_a_secret_crossing_the_truncation_is_redacted(self):
        # Reader report (#235087): masking after the 300-character cut kept
        # the prefix of a password sitting at the cut.
        secret = "LONG_SECRET_TOKEN"
        cfg = self.config(dsn="postgresql://agent:%s@localhost/mesh" % secret)
        with mock.patch.object(database_mod.config_mod, "load", return_value=cfg), \
                mock.patch.object(database_mod.db_mod, "connect",
                                  side_effect=db_mod.Unavailable(
                                      "x" * 292 + secret)):
            with self.assertRaises(database_mod.DatabaseUnavailable) as caught:
                database_mod.connect()
        message = str(caught.exception)
        self.assertNotIn("LONG_SEC", message)
        self.assertNotIn(secret, message)
        self.assertIn("***", message)

    def test_an_unknown_escaped_password_pair_is_redacted(self):
        message = database_mod._redact(
            "driver: password='first\\'TAIL_SECRET' host=db")
        self.assertNotIn("TAIL_SECRET", message)
        self.assertIn("password=***", message)

    def test_a_configuration_failure_is_unavailable(self):
        with mock.patch.object(database_mod.config_mod, "load",
                               side_effect=SystemExit(
                                   "config file not found: /srv/secret.json")):
            with self.assertRaises(database_mod.DatabaseUnavailable) as caught:
                database_mod.connect()
        self.assertEqual(caught.exception.code, "unavailable")
        self.assertIn("config file not found", str(caught.exception))

    def test_the_core_connection_is_returned_unwrapped_and_closes(self):
        connection = mock.Mock()
        with mock.patch.object(database_mod.config_mod, "load",
                               return_value=self.config()), \
                mock.patch.object(database_mod.db_mod, "connect",
                                  return_value=connection), \
                mock.patch.object(database_mod.db_mod, "require_schema"):
            result = database_mod.connect()
        self.assertIs(result, connection)


@unittest.skipUnless(support.REQUIRE_DB, "banc Postgres requis (AMEESH_REQUIRE_DB=0)")
class PublicConnectDatabaseTest(unittest.TestCase):
    """The public connection on a consumer-owned disposable database."""

    @classmethod
    def setUpClass(cls):
        if not bench_can_create_database():
            raise unittest.SkipTest(
                "bench role has no CREATEDB: ameesh.public.connect needs a "
                "migrated database to qualify against")
        cls.fixture = fixture_mod.Fixture(SCHEMA_ADMIN)
        cls.fixture.create()
        cls.addClassCleanup(cls.fixture.close)
        cls.fixture.canon(canon_files())
        synced = cls.fixture.cli("canon", "sync", "--json", "--host", HOST)
        if synced.returncode != 0:
            raise AssertionError("canon sync failed: %s%s"
                                 % ((synced.stdout or "")[-500:],
                                    (synced.stderr or "")[-500:]))
        try:
            document = json.loads(synced.stdout)
        except ValueError as exc:
            raise AssertionError("canon sync printed no JSON: %s"
                                 % (synced.stdout or "")[-500:]) from exc
        if document.get("errors"):
            raise AssertionError("canon sync refused: %r" % (document["errors"],))

    def environment(self, **extra):
        return self.fixture.env(**extra)

    def test_a_public_connection_serves_mail_and_closes(self):
        with mock.patch.dict(os.environ, self.environment(), clear=True):
            connection = database_mod.connect()
            try:
                receipt = public_mail.submit(
                    connection, ACTOR,
                    public_mail.Submission(PEER, "connect hello",
                                           idempotency_key="connect-usage-1"))
                page = public_mail.history(connection, ACTOR, peer=PEER)
                bodies = [entry.body for entry in page.entries]
                self.assertIn("connect hello", bodies)
                self.assertIn(receipt.id,
                              [entry.id for entry in page.entries])
                self.assertNotIn("postgresql://", repr(connection))
                self.assertNotIn("password=", repr(connection))
            finally:
                connection.close()
            connection.close()  # idempotent
            inner = getattr(connection, "conn", None)
            if inner is not None:
                self.assertTrue(inner.closed)

    def test_a_missing_schema_refuses_before_any_write(self):
        db = self.fixture.connect()
        before = len(public_mail.history(db, ACTOR).entries)
        absent = "connect_absent_%s" % os.urandom(4).hex()
        with mock.patch.dict(os.environ,
                             self.environment(AMEESH_SCHEMA=absent), clear=True):
            with self.assertRaises(database_mod.SchemaRefused) as caught:
                database_mod.connect()
        self.assertEqual(caught.exception.code, "schema")
        self.assertNotIn("postgresql://", str(caught.exception))
        after = len(public_mail.history(db, ACTOR).entries)
        self.assertEqual(before, after)

    def test_a_subprocess_in_the_fixture_environment_connects_with_the_public_surface(self):
        done = self.fixture.run(
            [sys.executable, "-c", CHILD_WITNESS],
            env={"CONNECT_ACTOR": ACTOR, "CONNECT_PEER": PEER})
        try:
            report = json.loads(done.stdout)
        except ValueError:
            self.fail("child printed no JSON (exit %d): %s%s"
                      % (done.returncode, (done.stdout or "")[-1000:],
                         (done.stderr or "")[-1000:]))
        self.assertEqual(done.returncode, 0, report)
        self.assertTrue(report.get("ok"), report)
        self.assertEqual(report["contract"], "1.6")
        self.assertEqual(report["mail_contract"], "1.4")
        self.assertIn("child connect hello", report["bodies"])
        self.assertNotIn("postgresql://", report["repr"])
        self.assertNotIn("password=", report["repr"])


class DeliberateBreakTest(unittest.TestCase):
    """A mutated connection surface is named by the frozen comparator."""

    def load(self, source):
        directory = tempfile.mkdtemp(prefix="w8333-break-")
        self.addCleanup(shutil.rmtree, directory, True)
        path = Path(directory) / "database.py"
        path.write_text(source, encoding="utf-8")
        suffix = re.sub(r"[^a-z0-9]", "", os.path.basename(directory).lower())
        name = "ameesh.public._database_break_%s" % suffix
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(name, None)
        return module

    def mutated(self, old, new):
        source = (ROOT / "src" / "ameesh" / "public" / "database.py").read_text(
            encoding="utf-8")
        self.assertEqual(source.count(old), 1, old)
        return self.load(source.replace(old, new))

    def test_a_new_required_parameter_is_detected(self):
        module = self.mutated(
            "def connect() -> Any:",
            "def connect(dsn) -> Any:")
        breaks = database_breaks(module)
        self.assertTrue(any("dsn: added without a default" in reason
                            for reason in breaks), breaks)

    def test_a_changed_published_revision_is_detected(self):
        module = self.mutated('CONTRACT_VERSION = "1.6"',
                              'CONTRACT_VERSION = "1.5"')
        breaks = database_breaks(module)
        self.assertTrue(any(reason.startswith("CONTRACT_VERSION:")
                            for reason in breaks), breaks)

    def test_a_removed_error_code_is_detected(self):
        module = self.mutated(
            'class SchemaRefused(DatabaseError):\n'
            '    """The database opened but its agent-mesh schema is not usable."""\n\n'
            '    code = "schema"',
            'class SchemaRefused(DatabaseError):\n'
            '    """The database opened but its agent-mesh schema is not usable."""\n\n'
            '    code = "error"')
        breaks = database_breaks(module)
        self.assertTrue(any(reason.startswith("SchemaRefused:") for reason in breaks),
                        breaks)


if __name__ == "__main__":
    unittest.main()
