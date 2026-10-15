# SPDX-License-Identifier: AGPL-3.0-only
"""#1193: a suite lot is born with its explicit parent (the blocking edge is
written in the same operation as the creation), and the L165 queue never offers
a lot whose explicit prerequisite is not finished."""
from __future__ import annotations

import unittest

from ameesh import registry, storage, work

from .support import PgTestCase
from .test_l165_prise_lots import _Lots


class CreationTest(PgTestCase):
    """Creating a suite lot links its parent in the same write."""

    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE work_items CASCADE")

    def edges(self, item_id):
        return storage.of(self.db).work_dependencies.for_items([item_id])

    def test_open_parent_is_linked_as_a_blocking_edge(self):
        parent = work.add(self.db, title="relu")
        suite = work.add(self.db, title="suite", parent_lot=parent["id"])
        self.assertEqual([(e["blocker_id"], e["blocking"]) for e in self.edges(suite["id"])],
                         [(parent["id"], True)])
        notes = [e["note"] for e in work.events(self.db, suite["id"])]
        self.assertIn("Dependency added: lot #%d" % parent["id"], notes)

    def test_unknown_parent_refuses_before_any_creation(self):
        before = [r["id"] for r in work.list_items(self.db)]
        with self.assertRaises(work.WorkError):
            work.add(self.db, title="orpheline", parent_lot=999999)
        with self.assertRaises(work.WorkError):
            work.add(self.db, title="négatif", parent_lot=-1)
        self.assertEqual([r["id"] for r in work.list_items(self.db)], before)

    def test_edge_failure_rolls_back_the_creation(self):
        # The edge is written in the creation transaction: a missing parent
        # leaves no suite lot exposed ready.
        before = [r["id"] for r in work.list_items(self.db)]
        with self.assertRaises(Exception):
            storage.of(self.db).work.add(
                type="evolution", source="", app="", title="fantôme", body="",
                issue_ref=None, workstream=None, assignee=None, budget_usd=None,
                note="created", actor="test", parent_lot=999999)
        self.assertEqual([r["id"] for r in work.list_items(self.db)], before)

    def test_finished_parent_is_compatible_and_traced(self):
        parent = work.add(self.db, title="relu")
        self.db.execute("UPDATE work_items SET state = 'merged' WHERE id = %s",
                        (parent["id"],))
        suite = work.add(self.db, title="suite", parent_lot=parent["id"])
        self.assertEqual(self.edges(suite["id"]), [])
        notes = [e["note"] for e in work.events(self.db, suite["id"])]
        self.assertTrue(any("already merged" in n for n in notes), notes)

    def test_mail_and_work_add_cli_carry_the_parent(self):
        for name in ("orch", "dev1"):
            registry.upsert(self.db, name, harness="claude", host=self.cfg.host)
        parent = work.add(self.db, title="relu")
        env = self.env(AMEESH_ALERT_ORCHESTRATORS="orch")
        proc = self.cli("send", "dev1", "constats N", "--from", "orch",
                        "--new-lot", "suite par courrier", "--estimate", "30m",
                        "--app", "p", "--new-project",
                        "--parent-lot", str(parent["id"]), env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        suite = [r for r in work.list_items(self.db, assignee="dev1")][0]
        self.assertEqual([(e["blocker_id"], e["blocking"]) for e in self.edges(suite["id"])],
                         [(parent["id"], True)])
        # #1070: the parent is also accepted as `#N`
        proc = self.cli("send", "dev1", "autre constat", "--from", "orch",
                        "--new-lot", "suite #N", "--estimate", "30m",
                        "--app", "p", "--new-project",
                        "--parent-lot", "#%d" % parent["id"], env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        suite2 = [r for r in work.list_items(self.db, assignee="dev1")
                  if r["title"] == "suite #N"][0]
        self.assertEqual([e["blocker_id"] for e in self.edges(suite2["id"])],
                         [parent["id"]])
        # `work add --parent-lot`
        proc = self.mesh("work", "add", "--title", "suite par la CLI",
                         "--parent-lot", str(parent["id"]), "--estimate", "30m")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        other = [r for r in work.list_items(self.db) if r["title"] == "suite par la CLI"][0]
        self.assertEqual([e["blocker_id"] for e in self.edges(other["id"])], [parent["id"]])
        # `--parent-lot` without `--new-lot`: refused, nothing is dropped
        proc = self.cli("send", "dev1", "sans suite", "--from", "orch",
                        "--parent-lot", str(parent["id"]), env=env)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--parent-lot goes with --new-lot", proc.stderr)


class ReadyIntakeTest(_Lots):
    """L165: an unfinished explicit prerequisite keeps the lot out of the ready intake."""

    def test_blocked_suite_is_not_offered_by_ready_intake(self):
        parent = self.lot("parent en qa")
        self.db.execute("UPDATE work_items SET state = 'qa' WHERE id = %s", (parent["id"],))
        suite = self.lot("suite du parent", parent_lot=parent["id"])
        ids = [i["id"] for i in storage.of(self.db).work.unassigned_lots(10)]
        self.assertEqual(ids, [])
        self.assertNotIn(suite["id"], ids)

    def test_agent_does_not_take_a_blocked_suite(self):
        self.agent("claude1", team="nexlink")
        parent = self.lot("parent")
        suite = self.lot("suite", parent_lot=parent["id"])
        out = self.take()
        self.assertEqual([t["item"]["id"] for t in out], [parent["id"]])
        self.assertIsNone(work.get(self.db, suite["id"])["assignee"])

    def test_edge_added_after_creation_keeps_the_lot_out_of_the_take(self):
        # Shape of case #286: the edge exists (written by the service), but the
        # L165 selector ignored it and took the lot that could not be started.
        from ameesh import work_dependencies as dependencies

        parent = self.lot("prérequis en build")
        self.db.execute("UPDATE work_items SET state = 'build' WHERE id = %s",
                        (parent["id"],))
        child = self.lot("suite qui attend")
        dependencies.add(self.db, child["id"], parent["id"], actor="test")
        ids = [i["id"] for i in storage.of(self.db).work.unassigned_lots(10)]
        self.assertNotIn(child["id"], ids)
        self.agent("claude1", team="nexlink")
        self.assertEqual(self.take(), [])

    def test_parent_merge_releases_the_suite(self):
        self.agent("claude1", team="nexlink")
        parent = self.lot("parent")
        suite = self.lot("suite", parent_lot=parent["id"])
        self.assertEqual([t["item"]["id"] for t in self.take()], [parent["id"]])
        self.assertIsNone(work.get(self.db, suite["id"])["assignee"])
        # The merged parent satisfies the edge: the suite becomes startable again.
        self.db.execute("UPDATE work_items SET state = 'merged' WHERE id = %s",
                        (parent["id"],))
        self.agent("claude2", team="nexlink")
        out = self.take()
        self.assertEqual([(t["kind"], t["item"]["id"]) for t in out], [("lot", suite["id"])])
        self.assertEqual(work.get(self.db, suite["id"])["assignee"], "claude2")


if __name__ == "__main__":
    unittest.main()
