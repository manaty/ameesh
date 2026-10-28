# SPDX-License-Identifier: AGPL-3.0-only
"""#4167 (manaty/ameesh#3185, volet 1): typed links between lots."""
from __future__ import annotations

import json
import threading
import unittest

from ameesh import db as D, storage, work
from ameesh import work_dependencies as dependencies
from tests.support import PgTestCase


class LinkStoreTest(PgTestCase):
    def setUp(self):
        self.db.execute("TRUNCATE work_items CASCADE")
        self.a, self.b, self.c = [work.add(self.db, title=title)["id"] for title in ("a", "b", "c")]
        self.store = storage.of(self.db).work_dependencies

    def link(self, a, b, kind):
        return self.store.add_link(a, b, kind, actor="test")

    def notes(self, lot):
        return [e["note"] for e in work.events(self.db, lot)
                if e["note"].startswith(("Link", "Conflict"))]

    def test_add_replay_remove_and_journal(self):
        self.assertEqual(self.link(self.a, self.b, "duplicates"), {"added": True})
        after = work.get(self.db, self.a)["updated_ts"]
        self.assertEqual(self.link(self.a, self.b, "duplicates"), {"added": False})
        self.assertEqual(work.get(self.db, self.a)["updated_ts"], after)
        # same pair, other kind: a distinct link
        self.assertEqual(self.link(self.a, self.b, "part_of"), {"added": True})
        seen = dependencies.links(self.db, [self.a, self.b])
        self.assertEqual(sorted(r["label"] for r in seen[self.a]), ["duplicates", "part of"])
        self.assertEqual(sorted(r["label"] for r in seen[self.b]), ["duplicated by", "has part"])
        self.assertTrue(self.store.remove_link(self.a, self.b, "duplicates", actor="test"))
        self.assertFalse(self.store.remove_link(self.a, self.b, "duplicates", actor="test"))
        self.assertEqual(self.notes(self.a), ["Link removed: duplicates lot #%d" % self.b,
                                              "Link added: part_of lot #%d" % self.b,
                                              "Link added: duplicates lot #%d" % self.b])
        # links are not prerequisites: the dependency graph is untouched
        self.assertEqual(self.store.for_items([self.a]), [])

    def test_self_unknown_and_cycles_per_kind_are_refused_without_mutation(self):
        self.assertEqual(self.link(self.a, self.a, "supersedes")["error"], "self_link")
        self.assertEqual(self.link(self.a, 999999, "supersedes")["error"], "unknown_lot")
        self.assertEqual(self.link(self.a, self.b, "supersedes"), {"added": True})
        self.assertEqual(self.link(self.b, self.c, "supersedes"), {"added": True})
        before = work.get(self.db, self.c)["updated_ts"]
        self.assertEqual(self.link(self.c, self.a, "supersedes")["error"], "link_cycle")
        self.assertEqual(self.link(self.b, self.a, "supersedes")["error"], "link_cycle")
        self.assertEqual(work.get(self.db, self.c)["updated_ts"], before)
        self.assertEqual(self.notes(self.c), [])
        # a cycle is judged inside one kind: another kind is another graph
        self.assertEqual(self.link(self.c, self.a, "part_of"), {"added": True})
        self.assertEqual(self.link(self.a, self.c, "part_of")["error"], "link_cycle")

    def test_conflicts_are_symmetric_and_need_two_open_lots(self):
        self.assertEqual(self.link(self.b, self.a, "conflicts_with"), {"added": True})
        self.assertEqual(self.link(self.a, self.b, "conflicts_with"), {"added": False})
        rows = self.store.links_for([self.a])
        self.assertEqual([(r["work_item_id"], r["other_id"]) for r in rows], [(self.a, self.b)])
        self.assertEqual(dependencies.links(self.db, [self.b])[self.b][0]["lot"], self.a)
        self.assertTrue(self.store.remove_link(self.a, self.b, "conflicts_with", actor="t"))
        work.close(self.db, self.c, abandoned=True)
        self.assertEqual(self.link(self.a, self.c, "conflicts_with")["error"], "finished_lot")
        # a duplicate of an already finished lot is a legitimate finding
        self.assertEqual(self.link(self.a, self.c, "duplicates"), {"added": True})

    def test_undecided_conflict_alerts_until_decided_or_closed(self):
        self.link(self.a, self.b, "conflicts_with")
        alerts = dependencies.alerts(self.db)
        self.assertEqual([(x["type"], x["lot"], x["other_lot"]) for x in alerts],
                         [("lot_conflict_undecided", self.a, self.b)])
        self.assertTrue(dependencies.decide_link(self.db, self.b, self.a, "keep #a, rescope #b",
                                                 actor="human:po"))
        self.assertFalse(dependencies.decide_link(self.db, self.a, self.b, "keep #a, rescope #b",
                                                  actor="human:po"))
        self.assertEqual(dependencies.alerts(self.db), [])
        row = dependencies.links(self.db, [self.a])[self.a][0]
        self.assertEqual((row["decision"], row["decided_by"]), ("keep #a, rescope #b", "human:po"))
        for lot, peer in ((self.a, self.b), (self.b, self.a)):
            self.assertIn("Conflict with lot #%d decided: keep #a, rescope #b" % peer,
                          self.notes(lot))
        # a new undecided conflict, then one end closes: the alert falls
        self.link(self.a, self.c, "conflicts_with")
        self.assertEqual(len(dependencies.alerts(self.db)), 1)
        work.close(self.db, self.c, abandoned=True)
        self.assertEqual(dependencies.alerts(self.db), [])
        with self.assertRaises(work.WorkError):
            dependencies.decide_link(self.db, self.b, self.c, "x", actor="human:po")

    def test_concurrent_opposite_links_cannot_both_commit(self):
        start = threading.Barrier(2)
        results, errors = [], []

        def add(a, b):
            db = D.connect(self.cfg)
            try:
                start.wait(timeout=5)
                results.append(storage.of(db).work_dependencies.add_link(
                    a, b, "supersedes", actor="test"))
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
        self.assertEqual(sorted(str(r) for r in results),
                         sorted([str({"added": True}), str({"error": "link_cycle"})]))

    def test_cli_edits_decides_and_refuses(self):
        def cli(*args, ok=True, lot=None):
            proc = self.mesh("work", "links", str(lot or self.a), *args, "--actor", "test")
            self.assertEqual(proc.returncode, 0 if ok else 1, proc.stdout + proc.stderr)
            return proc
        first = json.loads(cli("--add", str(self.b), "--kind", "duplicates", "--json").stdout)
        self.assertTrue(first["changed"])
        self.assertEqual(first["links"][0]["label"], "duplicates")
        self.assertFalse(json.loads(
            cli("--add", str(self.b), "--kind", "duplicates", "--json").stdout)["changed"])
        self.assertIn("need --kind", cli("--add", str(self.b), ok=False).stderr)
        self.assertIn("itself", cli("--add", str(self.a), "--kind", "part_of", ok=False).stderr)
        self.assertIn("cycle", cli("--add", str(self.a), "--kind", "duplicates",
                                   lot=self.b, ok=False).stderr)
        self.assertIn("#%d duplicated by #%d" % (self.b, self.a), cli(lot=self.b).stdout)
        cli("--add", str(self.c), "--kind", "conflicts_with")
        self.assertIn("undecided", cli(lot=self.c).stdout)
        self.assertIn("decision", cli("--decide", str(self.c), ok=False).stderr)
        cli("--decide", str(self.c), "--decision", "#c waits for #a")
        self.assertIn("decided by test: #c waits for #a", cli(lot=self.c).stdout)
        self.assertTrue(json.loads(
            cli("--remove", str(self.b), "--kind", "duplicates", "--json").stdout)["changed"])


class LinkServiceTest(unittest.TestCase):
    def test_invalid_kind_ids_and_actor_are_refused_before_storage(self):
        for call in (lambda: dependencies.add_link(None, 1, 2, "blocks", actor="x"),
                     lambda: dependencies.add_link(None, 0, 2, "duplicates", actor="x"),
                     lambda: dependencies.add_link(None, 1, 2, "duplicates", actor=" "),
                     lambda: dependencies.decide_link(None, 1, 2, " ", actor="x")):
            with self.assertRaises(work.WorkError):
                call()


if __name__ == "__main__":
    unittest.main()
