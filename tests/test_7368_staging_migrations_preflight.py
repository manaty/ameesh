# SPDX-License-Identifier: AGPL-3.0-only
"""#7368: the staging sequence gates the database's real pending migrations.

The generic gate is tested in test_7364; these tests cover the applicator-side
reader that turns the live `schema_migrations` ledger into the explicit frozen
inventory, and the refusal that must happen before any SQL write.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from ameesh import migrations
from ameesh import staging_migrations_preflight as preflight

ADDITIVE = "CREATE TABLE fresh (id integer);\n"
DESTRUCTIVE = "DROP TABLE legacy;\n"


class Ledger:
    """The read-only surface of a database connection used by `check`."""

    def __init__(self, applied=None):
        self.applied = dict(applied or {})
        self.queries = []
        self.writes = []
        self.closed = False

    def query(self, sql, params=None):
        self.queries.append(sql)
        if "to_regclass" in sql:
            return [{"ok": True}]
        if "schema_migrations" in sql:
            return [{"version": version, "checksum": checksum}
                    for version, checksum in sorted(self.applied.items())]
        raise AssertionError(sql)

    def execute(self, *args, **kwargs):
        self.writes.append((args, kwargs))

    def close(self):
        self.closed = True


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.directory = self.repo / migrations.REPO_PATH
        self.directory.mkdir(parents=True)
        self.git("init", "-q")
        self.git("config", "user.name", "test")
        self.git("config", "user.email", "test@localhost")
        self.put(".ameesh/staging-migrations.json",
                 json.dumps({"schema": "ameesh-staging-migrations/1",
                             "declarations": {}}))
        self.put("README", "fixture")

    def git(self, *args):
        return subprocess.check_output(
            ["git", "-C", str(self.repo), *args], stderr=subprocess.STDOUT).decode().strip()

    def put(self, path, text):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def migration(self, name, sql):
        path = migrations.REPO_PATH + "/" + name
        self.put(path, sql)
        return path, hashlib.sha256(sql.encode()).hexdigest()

    def commit(self):
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")
        return self.git("rev-parse", "HEAD")

    def check(self, ledger, sha, *, cfg=object()):
        with mock.patch.object(preflight.db_mod, "connect", return_value=ledger):
            return preflight.check(str(self.repo), sha, "ameesh", cfg=cfg,
                                   directory=str(self.directory))


class PendingInventoryTest(Fixture):
    def test_only_missing_migrations_are_sent_to_the_gate(self):
        applied, applied_sum = self.migration("9001_applied.sql", ADDITIVE)
        pending, _ = self.migration("9002_pending.sql", ADDITIVE)
        sha = self.commit()
        ledger = Ledger({9001: applied_sum})
        paths, unknown = preflight.pending_paths(ledger, str(self.directory))
        self.assertEqual(paths, [pending])
        self.assertEqual(unknown, [])
        # The applied file is not re-judged, even though it exists in the tree.
        self.assertNotIn(applied, paths)
        self.assertEqual(ledger.writes, [])
        self.assertTrue(ledger.closed is False)  # pending_paths does not own the connection

    def test_applied_migration_modified_is_refused_read_only(self):
        path, _ = self.migration("9001_applied.sql", ADDITIVE)
        self.commit()
        ledger = Ledger({9001: "0" * 64})
        with self.assertRaisesRegex(preflight.staging_migrations.Refused, "modified"):
            preflight.pending_paths(ledger, str(self.directory))
        self.assertEqual(ledger.writes, [])

    def test_unknown_applied_version_is_reported_not_refused(self):
        _, checksum = self.migration("9001_pending.sql", ADDITIVE)
        sha = self.commit()
        ledger = Ledger({9001: checksum, 9999: "a" * 64})
        report = self.check(ledger, sha)
        self.assertTrue(report["allowed"])
        self.assertEqual(report["pending"], [])
        self.assertEqual(report["unknown_applied"], [9999])

    def test_additive_pending_passes_without_policy_declaration(self):
        path, _ = self.migration("9001_add.sql", ADDITIVE)
        sha = self.commit()
        ledger = Ledger()
        report = self.check(ledger, sha)
        self.assertTrue(report["allowed"], report)
        self.assertEqual(report["pending"], [path])
        self.assertEqual(report["files"][0]["classification"], "additive")
        self.assertEqual(ledger.writes, [])
        self.assertTrue(ledger.closed)

    def test_undeclared_drop_stops_before_any_write(self):
        path, _ = self.migration("9001_drop.sql", DESTRUCTIVE)
        sha = self.commit()
        ledger = Ledger()
        report = self.check(ledger, sha)
        self.assertFalse(report["allowed"])
        self.assertEqual(report["pending"], [path])
        self.assertIn("destructive", report["files"][0]["reason"])
        self.assertIn("missing declaration", report["files"][0]["reason"])
        self.assertEqual(ledger.writes, [])
        self.assertTrue(ledger.closed)

    def test_gate_reads_the_frozen_tree_not_the_worktree(self):
        path, checksum = self.migration("9001_drop.sql", DESTRUCTIVE)
        sha = self.commit()
        # A local edit after the commit cannot turn a frozen DROP into additive.
        self.put(path, ADDITIVE)
        ledger = Ledger()
        report = self.check(ledger, sha)
        self.assertFalse(report["allowed"])
        self.assertIn("destructive", report["files"][0]["reason"])

    def test_declared_relaxation_passes(self):
        path, checksum = self.migration(
            "9001_relax.sql", "ALTER TABLE t DROP CONSTRAINT fk;\n")
        self.put("rollback/9001_restore.sql", "-- reviewed restore\n")
        self.put(".ameesh/staging-migrations.json", json.dumps({
            "schema": "ameesh-staging-migrations/1",
            "declarations": {path: {"checksum": checksum, "lot": 7368,
                                    "restore": "rollback/9001_restore.sql"}}}))
        sha = self.commit()
        report = self.check(Ledger(), sha)
        self.assertTrue(report["allowed"], report)
        self.assertEqual(report["files"][0]["classification"], "relaxation")


class CommandTest(unittest.TestCase):
    def test_refusal_is_nonzero_json_and_names_the_reason(self):
        report = {"schema": "ameesh-staging-migration-check/1", "sha": "a" * 40,
                  "allowed": False,
                  "files": [{"path": "src/ameesh/migrations/9001_drop.sql",
                             "allowed": False, "reason": "destructive DROP"}]}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(preflight, "check", return_value=report), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = preflight.main(["--repo", "/repo", "--sha", "a" * 40])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.getvalue())["allowed"], False)
        self.assertIn("refused", err.getvalue())
        self.assertIn("destructive DROP", err.getvalue())

    def test_success_prints_the_allow_report(self):
        report = {"schema": "ameesh-staging-migration-check/1", "sha": "a" * 40,
                  "allowed": True,
                  "files": [], "pending": [], "unknown_applied": []}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(preflight, "check", return_value=report), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = preflight.main(["--repo", "/repo", "--sha", "a" * 40])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out.getvalue())["allowed"])
        self.assertIn("0 pending migration(s) accepted", err.getvalue())


if __name__ == "__main__":
    unittest.main()
