# SPDX-License-Identifier: AGPL-3.0-only
"""L385 storage: explicit edges, atomic cycle checks and durable audit."""
from __future__ import annotations

import json
import threading
import time
import unittest

from ameesh import db as D, plan, planned_waits, progress, stagnation, storage, work
from ameesh import work_dependencies as dependencies
from tests.support import PgTestCase


class StoreTest(PgTestCase):
    def setUp(self):
        self.db.execute("TRUNCATE work_items CASCADE")
        self.a, self.b, self.c = [work.add(self.db, title=title)["id"] for title in ("a", "b", "c")]
        self.store = storage.of(self.db).work_dependencies

    def add(self, a, b):
        return self.store.add(a, b, actor="test")

    def test_add_replay_remove_and_journal(self):
        self.assertEqual(self.add(self.a, self.b), {"added": True})
        after = work.get(self.db, self.a)["updated_ts"]
        self.assertEqual(self.add(self.a, self.b), {"added": False})
        self.assertEqual(work.get(self.db, self.a)["updated_ts"], after)
        rows = self.store.for_items([self.a])
        self.assertEqual([(r["blocker_id"], r["blocking"]) for r in rows], [(self.b, True)])
        self.assertTrue(self.store.remove(self.a, self.b, actor="test"))
        self.assertFalse(self.store.remove(self.a, self.b, actor="test"))
        self.assertEqual(self.store.for_items([self.a]), [])
        notes = [e["note"] for e in work.events(self.db, self.a) if e["note"].startswith("Dependency")]
        self.assertEqual(notes, ["Dependency removed: lot #%d" % self.b,
                                 "Dependency added: lot #%d" % self.b])

    def test_self_unknown_and_transitive_cycle_are_refused_without_mutation(self):
        self.assertEqual(self.add(self.a, self.a)["error"], "self_dependency")
        self.assertEqual(self.add(self.a, 999999)["error"], "unknown_lot")
        self.assertEqual(self.add(self.a, self.b), {"added": True})
        self.assertEqual(self.add(self.b, self.c), {"added": True})
        before = work.get(self.db, self.c)["updated_ts"]
        self.assertEqual(self.add(self.c, self.a)["error"], "dependency_cycle")
        self.assertEqual(self.store.for_items([self.c]), [])
        self.assertEqual(work.get(self.db, self.c)["updated_ts"], before)

    def test_closed_blocker_keeps_edge_but_no_longer_blocks(self):
        self.add(self.a, self.b)
        work.close(self.db, self.b, abandoned=True)
        row = self.store.for_items([self.a])[0]
        self.assertFalse(row["blocking"])
        self.assertEqual(row["blocker_state"], "closed")
        self.assertEqual(self.add(self.a, self.b), {"added": False})
        self.assertEqual(self.add(self.c, self.b)["error"], "finished_lot")

    def test_concurrent_opposite_edges_cannot_both_commit(self):
        start = threading.Barrier(2)
        results, errors = [], []
        def add(a, b):
            db = D.connect(self.cfg)
            try:
                start.wait(timeout=5)
                results.append(storage.of(db).work_dependencies.add(a, b, actor="test"))
            except Exception as exc:
                errors.append(exc)
            finally:
                db.close()
        threads = [threading.Thread(target=add, args=pair, daemon=True)
                   for pair in ((self.a, self.b), (self.b, self.a))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(sum(r.get("added") is True for r in results), 1)
        self.assertEqual(sum(r.get("error") == "dependency_cycle" for r in results), 1)
        self.assertEqual(len(self.store.for_items([self.a, self.b])), 1)

    def test_graph_and_audit_roll_back_together(self):
        with self.assertRaises(RuntimeError):
            with self.db.transaction() as tx:
                self.assertEqual(storage.of(tx).work_dependencies.add(self.a, self.b, actor="test"),
                                 {"added": True})
                raise RuntimeError("rollback")
        self.assertEqual(self.store.for_items([self.a]), [])
        self.assertFalse(any(e["note"].startswith("Dependency") for e in work.events(self.db, self.a)))


    def age(self, item_id, now):
        self.db.execute("UPDATE work_items SET created_at = to_timestamp(%s),"
                        " updated_at = to_timestamp(%s) WHERE id = %s", (now - 40000, now - 40000, item_id))
        self.db.execute("UPDATE work_item_events SET created_at = to_timestamp(%s)"
                        " WHERE work_item_id = %s", (now - 40000, item_id))
        self.db.execute("UPDATE work_item_milestones SET at = to_timestamp(%s)"
                        " WHERE work_item_id = %s", (now - 40000, item_id))

    def test_explicit_wait_in_alerts_work_and_progress_releases_without_resetting_activity(self):
        dependencies.add(self.db, self.a, self.b, actor="test")
        self.db.execute("UPDATE work_items SET app = 'acme' WHERE id = %s", (self.a,))
        self.db.execute("UPDATE work_items SET app = 'other' WHERE id = %s", (self.b,))
        now = time.time()
        self.age(self.a, now)
        self.age(self.c, now)
        before = work.get(self.db, self.a)["updated_ts"]
        self.assertEqual([r["id"] for r in stagnation.stagnant_lots(self.db, now=now, limit=1)], [self.c])
        row = plan.annotate(self.db, [work.get(self.db, self.a)], now=now)[0]
        self.assertEqual(row["planned_wait"], {"what": "dependency", "lots": [self.b]})
        self.assertIsNone(row["stale"])
        self.assertTrue(row["dependencies"][0]["blocking"])
        snap = progress.snapshot(self.db, self.cfg, project="acme", now=now, max_lots=1, gantt=False)
        self.assertEqual(len(snap["lots"]), 1)
        self.assertEqual(snap["lots"][0]["dependencies"][0]["blocker_id"], self.b)
        self.assertIsNone(snap["lots"][0]["stale"])
        # Blocker is outside both the project's view and its rendering bound.
        work.close(self.db, self.b, abandoned=True)
        row = plan.annotate(self.db, [work.get(self.db, self.a)], now=now)[0]
        self.assertIsNone(row["planned_wait"])
        self.assertIsNotNone(row["stale"])
        self.assertFalse(row["dependencies"][0]["blocking"])
        self.assertEqual(work.get(self.db, self.a)["updated_ts"], before)
        self.assertIn(self.a, [r["id"] for r in stagnation.stagnant_lots(self.db, now=now)])
        snap = progress.snapshot(self.db, self.cfg, project="acme", now=now, gantt=False)
        self.assertFalse(snap["lots"][0]["dependencies"][0]["blocking"])
        self.assertIsNotNone(snap["lots"][0]["stale"])

    def test_cli_edits_replay_refusals_and_archived_prerequisites(self):
        def cli(*args, ok=True):
            proc = self.mesh("work", "dependencies", str(self.a), *args, "--actor", "test")
            self.assertEqual(proc.returncode, 0 if ok else 1, proc.stdout + proc.stderr)
            return proc
        first = json.loads(cli("--add", str(self.b), "--json").stdout)
        self.assertTrue(first["changed"])
        self.assertEqual(first["dependencies"][0]["created_by"], "test")
        before = work.get(self.db, self.a)["updated_ts"]
        self.assertFalse(json.loads(cli("--add", str(self.b), "--json").stdout)["changed"])
        self.assertEqual(work.get(self.db, self.a)["updated_ts"], before)
        self.assertIn("itself", cli("--add", str(self.a), ok=False).stderr)
        self.assertIn("existing lots", cli("--add", "999999", ok=False).stderr)
        proc = self.mesh("work", "dependencies", str(self.b), "--add", str(self.a), "--actor", "test")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("cycle", proc.stderr)
        show = self.mesh("work", "show", str(self.a), "--json")
        self.assertEqual(show.returncode, 0, show.stderr)
        self.assertEqual(json.loads(show.stdout)["planned_wait"]["lots"], [self.b])
        text = self.mesh("work", "list")
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertIn("waiting on lots #%d" % self.b, text.stdout)
        work.close(self.db, self.b, abandoned=True)
        self.assertIn("satisfied", cli().stdout)
        show = self.mesh("work", "show", str(self.a))
        self.assertEqual(show.returncode, 0, show.stderr)
        self.assertIn("depends  : #%d (closed, satisfied)" % self.b, show.stdout)
        self.assertTrue(json.loads(cli("--remove", str(self.b), "--json").stdout)["changed"])
        self.assertFalse(json.loads(cli("--remove", str(self.b), "--json").stdout)["changed"])
        self.assertEqual(json.loads(cli("--json").stdout)["dependencies"], [])


class ResolverTest(unittest.TestCase):
    def test_direct_links_union_with_commitments_and_ignore_satisfied_edges(self):
        rows = [{"id": 1, "state": "build"}]
        edges = [{"work_item_id": 1, "blocker_id": 2, "blocking": True},
                 {"work_item_id": 1, "blocker_id": 4, "blocking": False}]
        commitments = [{"status": "open", "work_item_id": 1, "depends_on": ["#3", "#2"]}]
        self.assertEqual(planned_waits.resolve(rows, {}, commitments, {},
            [{"id": 2, "state": "qa"}, {"id": 3, "state": "build"}], now=0, dependencies=edges),
            {1: {"what": "dependency", "lots": [2, 3]}})
        self.assertEqual(planned_waits.resolve(rows, {1: {"planned_start": "2999-01-01"}},
            [], {}, [], now=0, dependencies=edges),
            {1: {"what": "scheduled", "start": "2999-01-01"}})
        self.assertEqual(planned_waits.resolve([{ "id": 1, "state": "merged"}], {},
            [], {}, [], now=0, dependencies=edges), {})

    def test_invalid_service_ids_and_empty_actor_are_refused_before_storage(self):
        for item in (-1, 0, True, "1"):
            with self.assertRaises(work.WorkError):
                dependencies.add(None, item, 2, actor="test")
        with self.assertRaises(work.WorkError):
            dependencies.add(None, 1, 2, actor="")
