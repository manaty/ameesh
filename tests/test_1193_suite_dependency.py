# SPDX-License-Identifier: AGPL-3.0-only
"""#1193: a suite lot is born with its explicit parent (the blocking edge is
written in the same operation as the creation), and the L165 queue never offers
a lot whose explicit prerequisite is not finished."""
from __future__ import annotations

import threading
import time
import unittest
from unittest import mock

from ameesh import db as db_mod, registry, storage, work
from ameesh.storage.postgres import work as pg_work

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
        self.assertTrue(any("already delivered" in n for n in notes), notes)

    def test_mail_and_work_add_cli_carry_the_parent(self):
        # #540 : un agent ne reçoit qu'un seul lot non démarré ; les deux
        # envois de suite vont donc à deux agents distincts (dev1, puis dev2).
        for name in ("orch", "dev1", "dev2"):
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
        proc = self.cli("send", "dev2", "autre constat", "--from", "orch",
                        "--new-lot", "suite #N", "--estimate", "30m",
                        "--app", "p", "--new-project",
                        "--parent-lot", "#%d" % parent["id"], env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        suite2 = [r for r in work.list_items(self.db, assignee="dev2")
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


class LockOrderTest(PgTestCase):
    """Revue R1 delta #540 (auditeur) : `add` (suite, ou marque) et `assign`
    prennent les verrous dans le même ordre — les lignes de lots d'abord, le
    verrou d'agent ensuite. L'ordre inverse interbloquait les deux transactions
    (add d'une suite × assign de son parent), détecté et annulé par Postgres :
    le témoin échoue alors sur l'erreur rendue par l'une des transactions."""

    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE work_items CASCADE")
        for name in ("orch", "dev1"):
            registry.upsert(self.db, name, harness="claude", host=self.cfg.host)

    def edges(self, item_id):
        return storage.of(self.db).work_dependencies.for_items([item_id])

    def _wait_until_a_witness_waits(self, timeout=5.0) -> bool:
        """Attend qu'une transaction du témoin soit en attente d'un verrou."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rows = self.db.query(
                "SELECT count(*)::int AS n FROM pg_stat_activity"
                " WHERE wait_event_type = 'Lock' AND pid <> pg_backend_pid()")
            if int(rows[0]["n"]) > 0:
                return True
            time.sleep(0.05)
        return False

    def _scenario(self, add_call, assign_call):
        """T2 (`assign`) d'abord, retenu entre le verrou de sa ligne et le
        verrou d'agent (la fenêtre exacte du scénario) ; T1 (`add`) ensuite ;
        T2 est lâché dès que T1 attend un verrou. Rend les deux lots créés ou
        attribués ; toute erreur d'une transaction (interblocage) est rendue
        dans le bilan et fait échouer le témoin."""
        done1, done2 = {}, {}
        entered, release = threading.Event(), threading.Event()
        real_pending = pg_work.WorkItems._pending

        def run(call, done):
            conn = db_mod.connect(self.cfg)
            try:
                done["item"] = call(conn)
            except Exception as exc:  # noqa: BLE001 — le témoin doit voir l'interblocage
                done["error"] = "%s: %s" % (type(exc).__name__, exc)
            finally:
                conn.close()

        thread2 = threading.Thread(target=run, args=(assign_call, done2), daemon=True)
        thread1 = threading.Thread(target=run, args=(add_call, done1), daemon=True)

        def gated_pending(self_, assignee, item_id):
            if threading.current_thread() is thread2:
                entered.set()
                if not release.wait(10):
                    raise AssertionError("T2 n'a pas été lâchée")
            return real_pending(self_, assignee, item_id)

        with mock.patch.object(pg_work.WorkItems, "_pending", gated_pending):
            thread2.start()
            self.assertTrue(entered.wait(10), "T2 n'a pas pris le verrou de sa ligne")
            thread1.start()
            self.assertTrue(self._wait_until_a_witness_waits(),
                            "T1 n'a pas atteint son verrou")
            release.set()
            thread1.join(20)
            thread2.join(20)
        self.assertFalse(thread1.is_alive(), "T1 encore bloquée")
        self.assertFalse(thread2.is_alive(), "T2 encore bloquée")
        self.assertNotIn("error", done1, done1)
        self.assertNotIn("error", done2, done2)
        return done1["item"], done2["item"]

    def test_add_suite_et_assign_du_parent_sans_interblocage(self):
        parent = work.add(self.db, title="parent", actor="orch")

        def add_suite(conn):
            return work.add(conn, title="suite", app="ameesh", assignee="dev1",
                            actor="orch", parent_lot=int(parent["id"]))

        def assign_parent(conn):
            return work.assign(conn, int(parent["id"]), "dev1", actor="orch")

        suite, _ = self._scenario(add_suite, assign_parent)
        # une seule attribution non démarrée : la suite part dans la file
        self.assertEqual(work.get(self.db, int(parent["id"]))["assignee"], "dev1")
        self.assertIsNone(work.get(self.db, int(suite["id"]))["assignee"])
        self.assertEqual(suite["queued_behind"], int(parent["id"]))
        # l'arête bloquante est écrite avec la création
        self.assertEqual([(e["blocker_id"], e["blocking"]) for e in self.edges(suite["id"])],
                         [(int(parent["id"]), True)])

    def test_add_marque_et_assign_de_la_ligne_marquee_sans_interblocage(self):
        marked = work.add(self.db, title="parent relu", actor="orch")

        def add_marked(conn):
            return work.add(conn, title="relecture", app="ameesh", assignee="dev1",
                            actor="orch", mark_item_id=int(marked["id"]),
                            mark_note="relecture #{lot} ouverte")

        def assign_marked(conn):
            return work.assign(conn, int(marked["id"]), "dev1", actor="orch")

        review, _ = self._scenario(add_marked, assign_marked)
        self.assertEqual(work.get(self.db, int(marked["id"]))["assignee"], "dev1")
        self.assertIsNone(work.get(self.db, int(review["id"]))["assignee"])
        self.assertEqual(review["queued_behind"], int(marked["id"]))
        notes = [e["note"] for e in work.events(self.db, int(marked["id"]))]
        self.assertTrue(any("relecture #%d ouverte" % int(review["id"]) in (n or "")
                            for n in notes), notes)


if __name__ == "__main__":
    unittest.main()
