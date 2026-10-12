# SPDX-License-Identifier: AGPL-3.0-only
"""Project isolation and real execution evidence, including storage bypasses."""
from __future__ import annotations

import dataclasses
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from tests import support  # isolate host config before module tests
from tests.support import PgTestCase
from ameesh import canon, canon_sync, db as db_mod, modules, work
from ameesh.modules import it
from ameesh.modules.it.execution import OUTPUT_BYTES, digest, execute
from ameesh.modules.storage import ModuleStore


def federation(project="acme", names=None):
    return {"extensions": {"ameesh": {"projects": {
        project: {"modules": ["it"] if names is None else names}}}}}


class ModuleConfigurationTest(unittest.TestCase):
    def test_only_named_project_is_enabled(self):
        entries = modules.declarations(federation())
        self.assertEqual([(e["project"], e["module"]) for e in entries], [("acme", "it")])
        self.assertEqual(entries[0]["types"], ["release", "publication"])
        self.assertEqual(modules.declarations({}), [])
        self.assertEqual(modules.declarations(federation(names=[])), [])

    def test_unknown_duplicate_and_malformed_modules_refused(self):
        for names in (["not.installed"], ["it", "it"], "it", [3]):
            with self.subTest(names=names), self.assertRaises(ValueError):
                modules.declarations(federation(names=names))
        for raw in ({"extensions": []}, {"extensions": {"ameesh": []}},
                    {"extensions": {"ameesh": {"projects": []}}}):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                modules.declarations(raw)

    def test_canon_check_reports_module_errors(self):
        c = canon.Canon(root=".", readable=True, federation=federation(names=["missing"]))
        self.assertTrue(any(f.code == "project-modules-invalid" for f in canon.validate(c)))

    def test_refused_canon_does_not_apply_module_policies(self):
        c = canon.Canon(root=".", readable=True, federation=federation())
        lock = mock.Mock()
        with mock.patch.object(canon_sync, "last_applied", return_value=None), \
                mock.patch.object(canon_sync, "_verify", side_effect=canon_sync._Refused("bad ref")), \
                mock.patch.object(modules, "sync") as apply:
            result = canon_sync._sync_locked(lock, c, "main", "", "host")
        self.assertEqual(result.status, canon_sync.AUTH_REFUSED)
        apply.assert_not_called()


class ExecutionTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.artifact = Path(self.folder.name, "delivered.py")

    def write(self, source):
        self.artifact.write_text(source)
        return self.artifact

    def test_real_execution_binds_observed_digest_and_argv(self):
        self.write("import sys\nprint('started:', sys.argv[1])\n")
        evidence = execute(self.artifact, runner=sys.executable, arguments=["smoke"])
        self.assertEqual(evidence["sha256"], digest(self.artifact)[1])
        self.assertEqual(evidence["command"], [sys.executable, str(self.artifact), "smoke"])
        self.assertEqual(evidence["output"], "started: smoke")
        self.assertEqual(evidence["exit_code"], 0)

    def test_failed_silent_and_timeout_execution_are_not_proof(self):
        for source, error in (("raise SystemExit(2)", "failed"), ("pass", "no output"),
                              ("import time; time.sleep(5)", "timed out")):
            with self.subTest(error=error):
                self.write(source)
                with self.assertRaisesRegex(ValueError, error):
                    execute(self.artifact, runner=sys.executable,
                            timeout=.1 if error == "timed out" else 5)

    def test_changed_artifact_is_not_proof(self):
        self.write("from pathlib import Path\nPath(__file__).write_text('changed')\nprint('ok')\n")
        with self.assertRaisesRegex(ValueError, "changed during"):
            execute(self.artifact, runner=sys.executable)

    def test_output_is_drained_with_bounded_memory(self):
        self.write("print('x' * 2000000)\n")
        self.assertEqual(len(execute(self.artifact, runner=sys.executable)["output"]), OUTPUT_BYTES)

    def test_invalid_timeout_and_nonfile_are_refused(self):
        self.write("print('ok')")
        for timeout in (0, -1, float("nan"), float("inf"), 3601):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                execute(self.artifact, runner=sys.executable, timeout=timeout)
        with self.assertRaises(ValueError):
            digest(self.folder.name)

    def test_proof_requires_declaration_before_executing(self):
        self.write("print('ok')")
        with mock.patch.object(it, "item"), mock.patch.object(ModuleStore, "latest", return_value=None), \
                mock.patch("ameesh.modules.it.execution.execute") as run:
            with self.assertRaisesRegex(ValueError, "declare"):
                it.prove(mock.Mock(), 1, self.artifact, actor="test")
        run.assert_not_called()

    def test_wrong_digest_is_refused_before_executing(self):
        self.write("print('ok')")
        with mock.patch.object(it, "item"), \
                mock.patch.object(ModuleStore, "latest", return_value={"id": 1, "subject": "0" * 64}), \
                mock.patch("ameesh.modules.it.execution.execute") as run:
            with self.assertRaisesRegex(ValueError, "nothing executed"):
                it.prove(mock.Mock(), 1, self.artifact, actor="test")
        run.assert_not_called()


class LifecycleGateTest(PgTestCase):
    def setUp(self):
        super().setUp()
        with self.db.transaction() as tx:
            ModuleStore(tx).replace("", modules.declarations(federation()))
        self.artifact = Path(self.tmp, "artifact.py")
        self.artifact.write_text("print('delivered executable smoke passed')\n")
        self.lot = work.add(self.db, title="Publication", type="release", app="acme")["id"]
        self.store = ModuleStore(self.db)

    def prove(self):
        it.declare(self.db, self.lot, self.artifact, actor="tester")
        return it.prove(self.db, self.lot, self.artifact, actor="tester", runner=sys.executable)

    def test_project_isolation_and_unknown_type(self):
        with self.assertRaises(work.WorkError):
            work.add(self.db, title="Other project", type="release", app="other")
        with self.assertRaises(db_mod.DbError):
            self.db.execute("INSERT INTO work_items(title,type,app) VALUES ('raw','release','other')")
        core = work.add(self.db, title="Core", app="other")["id"]
        self.assertEqual(work.merged(self.db, core, sha="abcdef1234")["result"], "merged")

    def test_missing_proof_blocks_manual_and_automatic_closure_and_go(self):
        for action in (lambda: work.merged(self.db, self.lot, sha="abcdef1234"),
                       lambda: work.close_merged(self.db, self.lot, source="sync-github"),
                       lambda: work.milestone(self.db, self.lot, "verdict", verdict="ok")):
            with self.assertRaises(db_mod.DbError):
                action()
        self.assertEqual(work.get(self.db, self.lot)["state"], "intake")
        self.assertFalse(any(m["kind"] == "merged" for m in work.milestones(self.db, self.lot)))
        work.milestone(self.db, self.lot, "verdict", verdict="blocked")

    def test_delivery_without_proof_still_blocks_go(self):
        it.declare(self.db, self.lot, self.artifact, actor="tester")
        with self.assertRaisesRegex(db_mod.DbError, "proof"):
            work.milestone(self.db, self.lot, "verdict", verdict="ok")

    def test_matching_execution_allows_go_merge_and_promotion(self):
        proof = self.prove()
        self.assertEqual(proof["payload"]["exit_code"], 0)
        work.milestone(self.db, self.lot, "verdict", verdict="ok", sha="abcdef1234")
        self.assertEqual(work.merged(self.db, self.lot, sha="abcdef1234")["result"], "merged")
        self.assertEqual(work.move(self.db, self.lot, "promoted")["state"], "promoted")

    def test_work_move_and_raw_storage_cannot_bypass_gate(self):
        work.move(self.db, self.lot, "build")
        work.move(self.db, self.lot, "qa")
        with self.assertRaises(db_mod.DbError):
            work.move(self.db, self.lot, "merged")
        with self.assertRaises(db_mod.DbError):
            self.db.execute("UPDATE work_items SET state = 'promoted' WHERE id = %s", (self.lot,))
        self.assertEqual(work.get(self.db, self.lot)["state"], "qa")

    def test_new_declaration_invalidates_proof_even_for_same_bytes(self):
        self.prove()
        it.declare(self.db, self.lot, self.artifact, actor="tester")
        with self.assertRaisesRegex(db_mod.DbError, "proof"):
            work.close_merged(self.db, self.lot)
        it.prove(self.db, self.lot, self.artifact, actor="tester", runner=sys.executable)
        self.assertEqual(work.close_merged(self.db, self.lot)["result"], "merged")

    def test_changed_delivery_blocks_promotion(self):
        self.prove()
        work.merged(self.db, self.lot, sha="abcdef1234")
        self.artifact.write_text("print('new artifact')")
        it.declare(self.db, self.lot, self.artifact, actor="tester")
        with self.assertRaises(db_mod.DbError):
            work.move(self.db, self.lot, "promoted")

    def test_wrong_and_stale_binding_are_refused_atomically(self):
        self.prove()
        previous = self.store.latest(self.lot, "it", "delivery")
        it.declare(self.db, self.lot, self.artifact, actor="tester")
        evidence = execute(self.artifact, runner=sys.executable)
        with self.assertRaises(db_mod.DbError):
            self.store.record(self.lot, "it", "proof", evidence["sha256"], evidence,
                              actor="tester", binding_id=previous["id"])
        latest = self.store.latest(self.lot, "it", "delivery")
        evidence["sha256"] = "f" * 64
        with self.assertRaises(db_mod.DbError):
            self.store.record(self.lot, "it", "proof", "f" * 64, evidence,
                              actor="tester", binding_id=latest["id"])

    def test_module_milestones_are_immutable_and_failed_exit_rejected(self):
        proof = self.prove()
        for query in ("DELETE FROM work_module_milestones WHERE id = %s",
                      "UPDATE work_module_milestones SET subject = 'changed' WHERE id = %s"):
            with self.assertRaises(db_mod.DbError):
                self.db.execute(query, (proof["id"],))
        evidence = execute(self.artifact, runner=sys.executable)
        evidence["exit_code"] = 1
        with self.assertRaises(db_mod.DbError):
            self.store.record(self.lot, "it", "proof", evidence["sha256"], evidence,
                              actor="tester", binding_id=proof["binding_id"])

    def test_reclassification_cannot_erase_requirements(self):
        with self.assertRaises(db_mod.DbError):
            self.db.execute("UPDATE work_items SET type = 'evolution' WHERE id = %s", (self.lot,))
        with self.assertRaises(db_mod.DbError):
            self.db.execute("UPDATE work_items SET app = 'other' WHERE id = %s", (self.lot,))

    def test_another_canon_cannot_steal_project(self):
        with self.assertRaises(ValueError), self.db.transaction() as tx:
            ModuleStore(tx).replace("other", modules.declarations(federation()))
        self.assertTrue(self.store.enabled("acme", "it"))

    def test_cli_types_and_structured_milestones_are_visible(self):
        self.prove()
        proc = self.mesh("work", "show", str(self.lot), "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        import json
        self.assertEqual(len(json.loads(proc.stdout)["module_milestones"]), 2)
        proc = self.mesh("work", "add", "--title", "Store upload", "--type", "publication",
                         "--app", "acme", "--estimate", "1h", "--actor", "human:test")
        self.assertEqual(proc.returncode, 0, proc.stderr)
