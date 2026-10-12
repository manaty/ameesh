# SPDX-License-Identifier: AGPL-3.0-only
"""L385 storage: explicit edges, atomic cycle checks and durable audit."""
from __future__ import annotations

import threading
import unittest

from ameesh import db as D, storage, work
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
