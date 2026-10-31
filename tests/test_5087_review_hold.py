# SPDX-License-Identifier: AGPL-3.0-only
"""#5087: a held/withdrawn reviewer cannot create fresh review evidence."""
from unittest import mock

from ameesh import agent_review_hold, agent_rename, db as db_mod, registry, storage, work
from ameesh import review_designation as rd, review_designation_activation as activation
from ameesh import migrations
from .support import PgTestCase

HEAD = "a" * 40


class ReviewHoldTest(PgTestCase):
    def setUp(self):
        super().setUp()
        registry.upsert(self.db, "reader", harness="deepseek", host=self.cfg.host)
        self.db.execute("UPDATE agent_registry SET capabilities = ARRAY['propose'],"
                        " roles = ARRAY['implementation'], team = 'ameesh', status = 'idle'"
                        " WHERE name = 'reader'")
        self.lot = work.add(self.db, title="review hold regression", app="ameesh")["id"]
        self.db.execute("UPDATE work_items SET state = 'qa' WHERE id = %s", (self.lot,))
        patcher = mock.patch("ameesh.merge_queue.review_gate_for_item", return_value={"ok": False})
        patcher.start()
        self.addCleanup(patcher.stop)

    def hold(self, clear=False):
        return agent_review_hold.change(self.cfg, self.db, "reader", reason="N2 rename",
                                        clear=clear, actor="human:alice")

    def seat(self, remove=False):
        return storage.of(self.db).work.designate_reviewer(
            self.lot, "reader", actor="human:alice", remove=remove)

    def verdict(self, actor="reader"):
        return storage.of(self.db).work.add_milestone(
            self.lot, "verdict", sha=HEAD, actor=actor, verdict="ok", note="")

    def test_hold_is_visible_to_selection_and_manual_designation(self):
        before = registry.get(self.db, "reader")
        self.assertTrue(self.hold()["changed"])
        self.assertFalse(self.hold()["changed"])
        row = registry.get(self.db, "reader")
        self.assertEqual(row["review_hold"], {"actor": "human:alice", "reason": "N2 rename"})
        for key in ("status", "mode", "lease_owner", "lease_epoch"):
            self.assertEqual(row[key], before[key])
        listing = [r for r in registry.overview(self.db) if r["name"] == "reader"]
        self.assertEqual(rd.candidates_from_registry(listing, project="ameesh",
                         families=("claude", "codex", "deepseek")), [])
        self.assertFalse(rd.is_available(row))
        self.assertEqual(activation._unreachable_reviewer(row, set()), "review hold")
        self.assertTrue(storage.of(self.db).agents.reachability()[0]["review_hold"])
        with self.assertRaisesRegex(work.WorkError, "review hold"):
            work.check_reviewer(self.db, "reader")
        with self.assertRaisesRegex(db_mod.DbError, "review hold"):
            self.seat()
        self.assertTrue(self.hold(clear=True)["changed"])
        self.assertEqual(work.check_reviewer(self.db, "reader"), "reader")
        self.seat()

    def test_hold_rejects_verdict_without_receipt_or_milestone(self):
        self.seat()
        self.hold()
        before = storage.of(self.db).work.milestones(self.lot, None)
        with self.assertRaisesRegex(db_mod.DbError, "review hold"):
            self.verdict()
        self.assertEqual(storage.of(self.db).work.milestones(self.lot, None), before)
        self.hold(clear=True)
        self.verdict()

    def test_withdrawn_reviewer_refused_until_redesignated(self):
        self.seat()
        old = self.verdict()
        self.seat(remove=True)
        with self.assertRaisesRegex(db_mod.DbError, "withdrawn"):
            self.verdict()
        kept = next(m for m in storage.of(self.db).work.milestones(self.lot, None)
                    if m["id"] == old["id"])
        self.assertFalse(kept["annulled"])
        self.assertEqual(kept["verdict"], "ok")
        self.seat()
        self.verdict()
        self.seat(remove=True)
        with self.assertRaisesRegex(db_mod.DbError, "withdrawn"):
            self.verdict()

    def test_unsolicited_opinion_remains_possible(self):
        self.verdict()

    def test_carried_mark_cannot_reinstate_a_withdrawn_reviewer(self):
        self.seat()
        self.seat(remove=True)
        self.db.execute("UPDATE agent_registry SET aliases = ARRAY['old-reader'] WHERE name = 'reader'")
        self.assertEqual(agent_review_hold.carried_reviewers(
            self.db, self.lot, ["old-reader", "reader"]), [])
        effective = (1, "carried", {"reviewers": [{"name": "reader"}]})
        with mock.patch.object(activation, "_lock_current_freeze", return_value=True), \
                mock.patch.object(activation, "_marks", return_value=[]), \
                mock.patch.object(activation, "_effective_mark", return_value=effective), \
                mock.patch.object(activation, "_manual_designations", return_value={}):
            activation._sync_reviewers(self.db, {"id": self.lot}, {"id": 1}, ["reader"])
        with self.assertRaisesRegex(db_mod.DbError, "withdrawn"):
            self.verdict()
        self.assertEqual(work.get(self.db, self.lot)["reviewers"], [])

    def rotate(self, db=None):
        db = db or self.db
        registry.upsert(db, "next-reader", harness="claude", host=self.cfg.host)
        old = {"freeze": {"id": 1}, "reviewers": [{"name": "reader"}]}
        new = {"freeze": {"id": 2}, "reviewers": [{"name": "next-reader"}]}
        with mock.patch.object(activation, "_lock_current_freeze", return_value=True), \
                mock.patch.object(activation, "_marks", return_value=[(1, "selection", old, 0)]), \
                mock.patch.object(activation, "_effective_mark", return_value=(2, "selection", new)), \
                mock.patch.object(activation, "_manual_designations", return_value={}):
            activation._sync_reviewers(db, {"id": self.lot, "state": "qa"},
                                       {"id": 2, "sha": HEAD}, ["next-reader"])

    def test_rotation_keeps_old_reviewers_right_to_unsolicited_verdict(self):
        self.seat()
        self.rotate()
        self.assertEqual(work.get(self.db, self.lot)["reviewers"], ["next-reader"])
        self.assertEqual(self.db.query("SELECT * FROM work_review_withdrawals"
                                       " WHERE work_item_id = %s", (self.lot,)), [])
        self.verdict()

    def test_rotation_setting_does_not_leak_into_later_explicit_removal(self):
        self.seat()
        with self.db.transaction() as tx:
            self.rotate(tx)
            storage.of(tx).work.designate_reviewer(
                self.lot, "next-reader", actor="human:alice", remove=True)
        with self.assertRaisesRegex(db_mod.DbError, "withdrawn"):
            self.verdict(actor="next-reader")
        self.verdict()

    def test_rotation_rollback_restores_seat_and_setting(self):
        self.seat()
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            with self.db.transaction() as tx:
                self.rotate(tx)
                raise RuntimeError("rollback")
        self.assertEqual(work.get(self.db, self.lot)["reviewers"], ["reader"])
        self.seat(remove=True)
        with self.assertRaisesRegex(db_mod.DbError, "withdrawn"):
            self.verdict()

    def test_direct_removal_and_alias_cannot_bypass_withdrawal(self):
        self.seat()
        self.db.execute("UPDATE agent_registry SET aliases = ARRAY['old-reader'] WHERE name = 'reader'")
        self.db.execute("UPDATE work_items SET reviewers = '{}' WHERE id = %s", (self.lot,))
        with self.assertRaisesRegex(db_mod.DbError, "withdrawn"):
            self.verdict(actor="old-reader")

    def test_hold_and_withdrawal_follow_rename(self):
        self.seat()
        self.seat(remove=True)
        self.hold()
        agent_rename.rename(self.cfg, self.db, "reader", "renamed-reader",
                            reason="N2", actor="human:alice")
        self.assertTrue(registry.get(self.db, "renamed-reader")["review_hold"])
        self.assertEqual(self.db.query("SELECT reviewer FROM work_review_withdrawals"
                                       " WHERE work_item_id = %s", (self.lot,)),
                         [{"reviewer": "renamed-reader"}])
        with self.assertRaisesRegex(db_mod.DbError, "review hold"):
            self.verdict(actor="reader")

    def test_rename_of_a_designated_reviewer_is_not_a_withdrawal(self):
        self.seat()
        agent_rename.rename(self.cfg, self.db, "reader", "renamed-reader",
                            reason="N2", actor="human:alice")
        self.assertEqual(self.db.query("SELECT * FROM work_review_withdrawals"
                                       " WHERE work_item_id = %s", (self.lot,)), [])
        self.verdict(actor="renamed-reader")

    def test_unauthorized_actor_cannot_set_hold(self):
        with mock.patch.object(work, "corrector", side_effect=work.WorkError("refused")):
            with self.assertRaisesRegex(work.WorkError, "refused"):
                agent_review_hold.change(self.cfg, self.db, "reader", reason="N2", actor="reader")
        self.assertIsNone(registry.get(self.db, "reader")["review_hold"])

    def test_transaction_rollback_restores_the_review_seat_and_its_right_to_verdict(self):
        self.seat()
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            with self.db.transaction() as tx:
                tx.execute("UPDATE work_items SET reviewers = '{}' WHERE id = %s", (self.lot,))
                raise RuntimeError("rollback")
        self.verdict()

    def test_hold_clearing_does_not_restore_a_withdrawn_seat(self):
        self.seat()
        self.seat(remove=True)
        self.hold()
        self.hold(clear=True)
        with self.assertRaisesRegex(db_mod.DbError, "withdrawn"):
            self.verdict()

    def test_hold_event_records_set_and_clear_only_once(self):
        start = self.db.query("SELECT coalesce(max(id), 0) AS id FROM ameesh_events")[0]["id"]
        self.hold()
        self.hold()
        self.hold(clear=True)
        self.hold(clear=True)
        events = self.db.query("SELECT data FROM ameesh_events WHERE id > %s"
                               " AND type = 'agent.review_hold' ORDER BY id", (start,))
        self.assertEqual([e["data"]["cleared"] for e in events], [False, True])

    def test_uncommitted_hold_blocks_concurrent_verdict_then_refuses_it(self):
        other = db_mod.connect(self.cfg)
        self.addCleanup(other.close)
        with self.db.transaction() as tx:
            storage.of(tx).agents.review_hold("reader", reason="N2", actor="human:alice")
            with self.assertRaisesRegex(db_mod.DbError, "lock timeout"):
                with other.transaction() as concurrent:
                    concurrent.execute("SET LOCAL lock_timeout = '100ms'")
                    concurrent.execute("INSERT INTO work_item_milestones"
                                       " (work_item_id, kind, sha, actor, verdict)"
                                       " VALUES (%s, 'verdict', %s, 'reader', 'ok')",
                                       (self.lot, HEAD))
        with self.assertRaisesRegex(db_mod.DbError, "review hold"):
            storage.of(other).work.add_milestone(self.lot, "verdict", sha=HEAD,
                         actor="reader", verdict="ok", note="")

    def test_migration_is_additive_for_the_staging_gate(self):
        migration = next(m for m in migrations.discover() if m.name == "review_hold")
        self.assertEqual(migrations.non_additive(migration), [])
