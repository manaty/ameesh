# SPDX-License-Identifier: AGPL-3.0-only
"""#2179: reviewable roadmap edits and atomic, repeatable JSON exchange."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import re
import subprocess
import sys
import threading
import unittest
from unittest import mock

from .support import EMPTY_CONFIG, PgTestCase, child_env
from ameesh import roadmap, roadmap_edit as edit, storage, work


class RoadmapHelpTest(unittest.TestCase):
    def test_root_help_is_alphabetical_and_keeps_routes_visible(self):
        out = subprocess.run([sys.executable, "-m", "ameesh", "--help"],
            env=child_env(AMEESH_CONFIG=EMPTY_CONFIG, AMEESH_DSN="postgresql://none@127.0.0.1:1/none"),
            capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        headers = re.findall(r"^  ameesh (.+)$", out.stdout, re.M)
        self.assertGreater(len(headers), 60)
        self.assertEqual(headers, sorted(headers, key=str.casefold))
        self.assertEqual(len(headers), len(set(headers)))
        names = {line.split()[0] for line in headers}
        self.assertTrue({"plan", "approve", "approvals", "verify", "list", "show",
                         "migrate", "doctor", "import-v0", "export-v0", "version"} <= names)
        self.assertTrue({"ci", "models", "release", "review-class"} <= names)

    def test_editing_help_never_connects_to_database(self):
        for command in ("add", "get", "update", "import", "export", "validate"):
            with self.subTest(command=command):
                out = subprocess.run([sys.executable, "-m", "ameesh", "plan", command, "--help"],
                    env=child_env(AMEESH_CONFIG=EMPTY_CONFIG, AMEESH_DSN="postgresql://none@127.0.0.1:1/none"),
                    capture_output=True, text=True, timeout=30)
                self.assertEqual(out.returncode, 0, out.stderr)
                self.assertIn("usage: ameesh plan " + command, out.stdout)

    def test_malformed_json_is_rejected_without_storage(self):
        cases = ["[]", '{"schema":"ameesh-plan/1","commitments":{},"extra":1}',
            '{"schema":"ameesh-plan/1","commitments":[],"commitments":[]}',
            json.dumps({"schema": edit.SCHEMA, "commitments": [{"key": "a"}, {"key": "a"}]}),
            json.dumps({"schema": edit.SCHEMA, "commitments": [{"key": "a", "unknown": 1}]})]
        for text in cases:
            with self.subTest(text=text), self.assertRaises(roadmap.RoadmapError):
                edit.document(text)


class RoadmapEditingPgTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE commitments RESTART IDENTITY CASCADE")

    def rows(self):
        return storage.of(self.db).roadmap.commitments()

    def cli_json(self, *args):
        out = self.mesh("plan", *args, "--json")
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def plan_file(self, entries):
        path = Path(self.tmp) / "plan.json"
        path.write_text(json.dumps({"schema": edit.SCHEMA, "commitments": entries}), encoding="utf-8")
        return str(path)

    def test_add_read_edit_accept_and_terminal_protection(self):
        lot = work.add(self.db, title="Acme planning", app="acme", assignee="human:planner")
        note = "Functional scope\n\nFirst line: épreuve.\nLast line.\n"
        path = Path(self.tmp) / "note.md"
        path.write_text(note, encoding="utf-8")
        row = self.cli_json("add", "Specify the module", "--proposed", "--key", "ctx-01",
                            "--lot", "#" + str(lot["id"]), "--note-file", str(path), "--actor", "human:owner")
        self.assertEqual((row["status"], row["due_on"], row["project"], row["owner"]),
                         ("proposed", None, "acme", "human:planner"))
        row = self.cli_json("get", "e1")
        self.assertEqual(row["note"], note)
        token = row["etag"]
        changed = self.cli_json("update", "e1", "--due", "2026-12-01", "--owner", "-",
                                "--what", "Specify and qualify the module", "--if-match", token)
        self.assertIsNone(changed["owner"])
        self.assertEqual(changed["created_by"], "human:owner")
        self.assertEqual(changed["proposal_key"], "ctx-01")
        self.assertEqual(changed["created_ts"], row["created_ts"])
        stale = self.mesh("plan", "update", "e1", "--note", "stale", "--if-match", token)
        self.assertEqual(stale.returncode, 1, stale.stdout)
        self.assertIn("changed", stale.stderr)
        self.assertEqual(self.rows()[0]["note"], note)
        self.assertEqual(self.mesh("plan", "accept", "e1").returncode, 0)
        self.assertEqual(self.mesh("plan", "done", "e1").returncode, 0)
        before = self.rows()
        refused = self.mesh("plan", "update", "e1", "--note", "rewrite history")
        self.assertEqual(refused.returncode, 1)
        self.assertIn("terminal", refused.stderr)
        self.assertEqual(self.rows(), before)

    def test_stable_add_key_does_not_duplicate_or_silently_overwrite(self):
        args = ("add", "Prepare a proposal", "--proposed", "--key", "repeatable")
        first = self.cli_json(*args)
        again = self.cli_json(*args)
        self.assertEqual(again["id"], first["id"])
        self.assertTrue(again["unchanged"])
        refused = self.mesh("plan", "add", "Different proposal", "--proposed", "--key", "repeatable")
        self.assertEqual(refused.returncode, 1)
        self.assertEqual(len(self.rows()), 1)

    def test_forward_references_preview_apply_repeat_export_and_stdin(self):
        entries = [{"key": "phase-2", "what": "Qualify", "due": "2026-12-01", "depends_on": ["@phase-1"]},
                   {"key": "phase-1", "what": "Specify", "due": "2026-12-02", "note": "Scope\nProcess\nRole"}]
        filename = self.plan_file(entries)
        before = self.db.query("SELECT (SELECT count(*) FROM work_items) AS lots, "
                               "(SELECT count(*) FROM agent_registry) AS agents")
        preview = self.cli_json("import", filename)
        self.assertFalse(preview["applied"])
        self.assertEqual([c["action"] for c in preview["changes"]], ["create", "create"])
        self.assertEqual(preview["warnings"][0]["code"], "dependency_due_later")
        self.assertEqual(self.rows(), [])
        validation = self.cli_json("validate", filename)
        self.assertTrue(validation["valid"])
        self.assertEqual(validation["schema"], "ameesh-plan-validation/1")
        applied = self.cli_json("import", filename, "--apply", "--actor", "human:owner")
        self.assertTrue(applied["applied"])
        by_key = {r["proposal_key"]: r for r in self.rows()}
        self.assertEqual(by_key["phase-2"]["depends_on"], ["e%d" % by_key["phase-1"]["id"]])
        stored = self.rows()
        again = self.cli_json("import", filename, "--apply")
        self.assertEqual([c["action"] for c in again["changes"]], ["unchanged", "unchanged"])
        self.assertEqual(self.rows(), stored)
        exported = self.mesh("plan", "export")
        self.assertEqual(exported.returncode, 0, exported.stderr)
        document = json.loads(exported.stdout)
        self.assertIn("@phase-1", next(e for e in document["commitments"] if e["key"] == "phase-2")["depends_on"])
        stdin = subprocess.run([sys.executable, "-m", "ameesh", "plan", "import", "-", "--apply", "--json"],
            input=exported.stdout, env=self.env(), cwd=self.tmp, capture_output=True, text=True, timeout=60)
        self.assertEqual(stdin.returncode, 0, stdin.stderr)
        self.assertTrue(all(c["action"] == "unchanged" for c in json.loads(stdin.stdout)["changes"]))
        outfile = Path(self.tmp) / "export.json"
        self.assertEqual(self.mesh("plan", "export", "--output", str(outfile)).returncode, 0)
        self.assertEqual(json.loads(outfile.read_text()), document)
        self.assertEqual(self.db.query("SELECT (SELECT count(*) FROM work_items) AS lots, "
                                       "(SELECT count(*) FROM agent_registry) AS agents"), before)
        self.assertEqual(self.turns(), [])

    def test_import_requires_explicit_update_and_preserves_identity(self):
        original = edit.create(self.db, {"what": "Initial", "key": "a", "note": "keep"}, actor="original")
        filename = self.plan_file([{"key": "a", "what": "Edited"}])
        out = self.mesh("plan", "import", filename, "--apply")
        self.assertEqual(out.returncode, 1)
        self.assertEqual(self.rows()[0]["what"], "Initial")
        edit_result = self.cli_json("import", filename, "--apply", "--update", "--actor", "editor")
        self.assertEqual(edit_result["changes"][0]["action"], "update")
        row = self.rows()[0]
        self.assertEqual((row["id"], row["created_ts"], row["created_by"], row["note"]),
                         (original["id"], original["created_ts"], "original", "keep"))
        filename = self.plan_file([{"key": "a", "status": "open", "due": "2026-12-03"}])
        self.cli_json("import", filename, "--apply", "--update")
        filename = self.plan_file([{"key": "a", "status": "proposed"}])
        self.assertEqual(self.mesh("plan", "import", filename, "--apply", "--update").returncode, 1)

    def test_unknown_references_and_cycles_leave_database_unchanged(self):
        invalid = [
            [{"key": "a", "what": "Unknown", "depends_on": ["e999"]}],
            [{"key": "a", "what": "Unknown", "depends_on": ["@missing"]}],
            [{"key": "a", "what": "Unknown", "depends_on": ["task #999"]}],
            [{"key": "a", "what": "Unknown", "depends_on": ["missing-package"]}],
            [{"key": "a", "what": "A", "depends_on": ["@b"]},
             {"key": "b", "what": "B", "depends_on": ["@a"]}],
            [{"key": "a", "what": "Valid"}, {"key": "b", "what": "Invalid", "due": "tomorrow"}],
            [{"key": "a", "what": "Invalid", "depends_on": "e1"}],
            [{"key": "a", "what": "Invalid", "status": "done"}],
        ]
        for entries in invalid:
            with self.subTest(entries=entries):
                filename = self.plan_file(entries)
                out = self.mesh("plan", "import", filename, "--apply")
                self.assertEqual(out.returncode, 1, out.stdout)
                self.assertEqual(self.rows(), [])
        a = edit.create(self.db, {"what": "A"})
        b = edit.create(self.db, {"what": "B", "depends_on": ["e%d" % a["id"]]})
        out = self.mesh("plan", "update", str(a["id"]), "--depends-on", "e%d" % b["id"])
        self.assertEqual(out.returncode, 1)
        self.assertIn("dependency_cycle", out.stderr)
        self.assertEqual(edit.get(self.db, a["id"])["depends_on"], [])
        filename = self.plan_file(invalid[0])
        out = self.mesh("plan", "validate", filename, "--json")
        self.assertEqual(out.returncode, 1)
        report = json.loads(out.stdout)
        self.assertFalse(report["valid"])
        self.assertEqual(report["errors"][0]["code"], "unknown_dependency")

    def test_import_rolls_back_if_write_fails_after_identity_allocation(self):
        implementation = type(storage.of(self.db).roadmap)
        original = implementation.set_commitment
        calls = []

        def fail_second_write(adapter, *args, **kwargs):
            calls.append(args[0])
            if len(calls) == 2:
                raise roadmap.RoadmapError("simulated write failure")
            return original(adapter, *args, **kwargs)

        body = {"schema": edit.SCHEMA, "commitments": [{"key": "a", "what": "A"}, {"key": "b", "what": "B"}]}
        with mock.patch.object(implementation, "set_commitment", autospec=True, side_effect=fail_second_write):
            with self.assertRaisesRegex(roadmap.RoadmapError, "simulated write failure"):
                edit.import_plan(self.db, body, apply=True)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.rows(), [])

    def test_storage_compare_and_swap_rejects_a_stale_snapshot(self):
        row = edit.create(self.db, {"what": "Original"})
        edit.update(self.db, row["id"], {"note": "new note"})
        refused = storage.of(self.db).roadmap.set_commitment(row["id"], current=roadmap.LIVE_STATUSES,
                                                            values={"what": "stale"}, expect=row)
        self.assertIsNone(refused)
        self.assertEqual(self.rows()[0]["what"], "Original")
        self.assertEqual(self.rows()[0]["note"], "new note")

    def test_concurrent_edits_cannot_create_a_dependency_cycle(self):
        a = edit.create(self.db, {"what": "A"})
        b = edit.create(self.db, {"what": "B"})
        start = threading.Barrier(2)

        def change(source, target):
            connection = self.connect()
            try:
                start.wait(timeout=10)
                edit.update(connection, source, {"depends_on": ["e%d" % target]})
                return "updated"
            except roadmap.RoadmapError as exc:
                return str(exc)
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [future.result(timeout=30) for future in
                       [pool.submit(change, a["id"], b["id"]), pool.submit(change, b["id"], a["id"])]]
        self.assertEqual(results.count("updated"), 1, results)
        self.assertTrue(any("dependency_cycle" in result for result in results), results)
        self.assertTrue(edit.validate(self.db)["valid"])

    def test_legacy_unkeyed_export_is_repeatable_and_terminal_rows_stay_closed(self):
        row = roadmap.add_commitment(self.db, "Legacy", due="2026-12-01")
        body = edit.export(self.db)
        self.assertTrue(body["commitments"][0]["key"].startswith("export:"))
        self.assertEqual(edit.import_plan(self.db, body, apply=True)["changes"][0]["action"], "unchanged")
        self.assertIsNone(self.rows()[0]["proposal_key"])
        roadmap.settle(self.db, row["id"], "done")
        body = edit.export(self.db, all_states=True)
        self.assertEqual(edit.import_plan(self.db, body, apply=True)["changes"][0]["action"], "unchanged")
        body["commitments"][0]["note"] = "rewrite history"
        with self.assertRaisesRegex(roadmap.RoadmapError, "terminal"):
            edit.import_plan(self.db, body, apply=True, update_existing=True)
        self.assertEqual(len(self.rows()), 1)

    def test_export_import_preserves_equivalent_legacy_reference_spellings(self):
        first = roadmap.add_commitment(self.db, "First", due="2026-12-01")
        spelling = "commitment E%d" % first["id"]
        roadmap.add_commitment(self.db, "Second", due="2026-12-02", depends_on=[spelling])
        before = self.rows()
        report = edit.import_plan(self.db, edit.export(self.db), apply=True)
        self.assertEqual([c["action"] for c in report["changes"]], ["unchanged", "unchanged"])
        self.assertEqual(self.rows(), before)

    def test_project_filter_precedes_limit_and_excludes_foreign_commitments(self):
        other = [work.add(self.db, title="Other %d" % i, app="other") for i in range(3)]
        wanted = [work.add(self.db, title="Acme %d" % i, app="acme") for i in range(2)]
        roadmap.add_commitment(self.db, "Foreign", due="2026-12-01", lot=other[0]["id"])
        roadmap.add_commitment(self.db, "Global", due="2026-12-01")
        roadmap.add_commitment(self.db, "Acme", due="2026-12-01", lot=wanted[1]["id"])
        view = roadmap.build(self.db, project="acme", max_items=1)
        self.assertEqual([t["id"] for t in view["tasks"]], [wanted[0]["id"]])
        self.assertEqual({c["what"] for c in view["commitments"]}, {"Acme", "Global"})
        rows = storage.of(self.db).roadmap.items(since_ts=0, limit=1, done_states=["closed"], project="acme")
        self.assertEqual(rows[0]["total"], 2)
        self.assertEqual({c["what"] for c in self.cli_json("list", "--project", "acme")}, {"Acme", "Global"})
        self.assertEqual({c["what"] for c in edit.export(self.db, project="acme")["commitments"]}, {"Acme", "Global"})

    def test_validation_reports_stored_unknown_references_and_warnings(self):
        a = roadmap.add_commitment(self.db, "A", due="2026-12-02")
        roadmap.add_commitment(self.db, "B", due="2026-12-01", depends_on=["e%d" % a["id"]])
        report = self.cli_json("validate")
        self.assertTrue(report["valid"])
        self.assertEqual(report["warnings"][0]["code"], "dependency_due_later")
        roadmap.add_commitment(self.db, "Unknown", due="2026-12-03", depends_on=["e999"])
        out = self.mesh("plan", "validate", "--json")
        self.assertEqual(out.returncode, 1)
        self.assertEqual(json.loads(out.stdout)["errors"][0]["code"], "unknown_dependency")


if __name__ == "__main__":
    unittest.main()
