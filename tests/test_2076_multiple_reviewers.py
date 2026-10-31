# SPDX-License-Identifier: AGPL-3.0-only
"""Multiple designations, frozen-head verdicts and rolling runner upgrades."""
from __future__ import annotations

import concurrent.futures
import threading
import unittest
from pathlib import Path

from .support import PgTestCase, REPO
from ameesh import alert_briefs, progress, stagnation, storage, work


class ReviewWaitingTest(unittest.TestCase):
    def marks(self, *verdicts, frozen="a" * 40, reviewers=None, agents=None):
        reviewers = list(("first", "second") if reviewers is None else reviewers)
        item = {"state": "qa", "assignee": "author",
                "reviewer": reviewers[-1] if reviewers else "", "reviewers": reviewers}
        rows = [{"kind": "frozen", "at_ts": 1, "sha": frozen}]
        rows += [{"kind": "verdict", "at_ts": i + 2, "actor": actor,
                  "verdict": verdict, "sha": sha}
                 for i, (actor, verdict, sha) in enumerate(verdicts)]
        return item, progress._work_item_milestones(item, [], [], rows, agents=agents)

    def test_each_reviewer_owes_a_current_verdict(self):
        a = "a" * 40
        item, marks = self.marks(("first", "ok", a))
        self.assertEqual(stagnation.waiting(item, marks)["who"], "second")
        self.assertEqual(progress.lot_state("qa", marks), "review")
        item, marks = self.marks(("first", "ok", a), ("second", "ok", "b" * 40))
        self.assertEqual(stagnation.waiting(item, marks)["who"], "second")
        item, marks = self.marks(("first", "ok", a), ("second", "ok", a))
        self.assertEqual(stagnation.waiting(item, marks)["what"], "merge")
        self.assertEqual(progress.lot_state("qa", marks), "approved")
        item, marks = self.marks(("first", "ok", a), ("second", "ok", a), frozen="c" * 40)
        self.assertEqual(stagnation.waiting(item, marks)["who"], "first, second")

    def test_later_go_does_not_erase_another_reviewers_blocker(self):
        a = "a" * 40
        item, marks = self.marks(("first", "blocked", a), ("second", "ok", a))
        self.assertEqual(stagnation.waiting(item, marks)["what"], "fix")
        self.assertEqual(progress.lot_state("qa", marks), "review")
        item, marks = self.marks(("first", "blocked", a), ("second", "ok", a),
                                 ("first", "ok", a))
        self.assertEqual(stagnation.waiting(item, marks)["what"], "merge")

    def test_both_pending_reviewers_receive_the_alert(self):
        alert = {"type": "stale_lot", "agent": "author", "lot": 42,
                 "title": "review", "lot_state": "qa", "value": 25000,
                 "project": "ameesh", "orchestrators": [],
                 "waiting": {"what": "verdict", "who": "first, second",
                             "label": "verdict by first, second"}}
        briefs = alert_briefs.briefs(alert, [{"name": n, "team": "ameesh"}
                                            for n in ("author", "first", "second")])
        self.assertIn("first", briefs)
        self.assertIn("second", briefs)

    def test_delayed_other_head_verdict_preserves_current_blocker(self):
        a, b = "a" * 40, "b" * 40
        item, marks = self.marks(("first", "blocked", a), ("second", "ok", a),
                                 ("first", "ok", b))
        self.assertEqual(stagnation.waiting(item, marks)["what"], "fix")
        self.assertEqual(marks["verdict_kind"], "blocked")
        self.assertTrue(marks["verdict_current"])

    def test_delayed_other_head_verdict_preserves_completed_reviews(self):
        a, b = "a" * 40, "b" * 40
        for actor in ("first", "third"):
            with self.subTest(actor=actor):
                item, marks = self.marks(("first", "ok", a), ("second", "ok", a),
                                         (actor, "ok", b))
                self.assertEqual(marks["pending_reviewers"], [])
                self.assertEqual(stagnation.waiting(item, marks)["what"], "merge")
                self.assertEqual(progress.lot_state("qa", marks), "approved")

    def test_blocked_verdict_of_a_withdrawn_reviewer_does_not_hold_the_lot(self):
        a = "a" * 40
        verdicts = [("first", "blocked", a), ("second", "ok", a)]
        for ordered in (verdicts, verdicts[::-1]):
            with self.subTest(ordered=ordered):
                item, marks = self.marks(*ordered, reviewers=["second"])
                self.assertEqual(stagnation.waiting(item, marks)["what"], "merge")
                self.assertEqual(progress.lot_state("qa", marks), "approved")
                self.assertEqual(marks["pending_reviewers"], [])

    def test_no_designation_or_waiver_keeps_a_current_blocker(self):
        # #1979 : sans relecteur désigné (ou avec la dispense `-none`), un
        # verdict bloqué courant demande une correction, pas une dispense.
        for reviewers in ([], ["-none"]):
            with self.subTest(reviewers=reviewers):
                item, marks = self.marks(("first", "blocked", "a" * 40),
                                         reviewers=reviewers)
                self.assertTrue(marks["verdict_current"])
                self.assertEqual(marks["verdict_kind"], "blocked")
                # `waiting` nomme la correction due ; `lot_state` laisse le
                # lot « review » (un bloquant courant n'est jamais approuvé).
                self.assertEqual(progress.lot_state("qa", marks), "review")
                self.assertEqual(stagnation.waiting(item, marks)["what"], "fix")

    def test_human_prefixed_actor_matches_a_bare_designation(self):
        # #2204/N4 : un humain peut être nommé sans préfixe ; #2346 ne garde
        # cet alias que si le nom nu n'est pas un agent du registre.
        a = "a" * 40
        item, marks = self.marks(("first", "ok", a), ("human:second", "ok", a),
                                 agents=set())
        self.assertEqual(marks["pending_reviewers"], [])
        self.assertEqual(stagnation.waiting(item, marks)["what"], "merge")
        item, marks = self.marks(("first", "ok", a), ("second", "ok", a),
                                 reviewers=["first", "human:second"], agents=set())
        self.assertEqual(marks["pending_reviewers"], [])
        self.assertEqual(stagnation.waiting(item, marks)["what"], "merge")

    def test_agent_homonym_is_not_the_human_of_the_same_name(self):
        """#2346 : `second` agent ne vaut ni l'approbation `human:second`, ni
        le verdict attendu du relecteur `human:second`."""
        a = "a" * 40
        item, marks = self.marks(("first", "ok", a), ("human:second", "ok", a),
                                 reviewers=["first", "second"], agents={"second"})
        self.assertEqual(marks["pending_reviewers"], ["second"])
        self.assertEqual(stagnation.waiting(item, marks)["who"], "second")
        self.assertEqual(progress.lot_state("qa", marks), "review")
        item, marks = self.marks(("first", "ok", a), ("second", "ok", a),
                                 reviewers=["first", "human:second"], agents={"second"})
        self.assertEqual(marks["pending_reviewers"], ["human:second"])
        self.assertEqual(stagnation.waiting(item, marks)["who"], "human:second")
        # Sans classification (agents=None), l'alias n'est pas deviné.
        item, marks = self.marks(("first", "ok", a), ("human:second", "ok", a))
        self.assertEqual(marks["pending_reviewers"], ["second"])

    def test_human_aliases_share_the_latest_current_verdict(self):
        a = "a" * 40
        for bare, prefixed in (("second", "human:second"), ("human:second", "second")):
            for latest, expected in (("ok", "merge"), ("blocked", "fix")):
                with self.subTest(bare=bare, latest=latest):
                    prior = "blocked" if latest == "ok" else "ok"
                    item, marks = self.marks((bare, prior, a), (prefixed, latest, a),
                                             reviewers=[bare], agents=set())
                    self.assertEqual(stagnation.waiting(item, marks)["what"], expected)
                    self.assertEqual(marks["verdict_kind"], latest)

    def test_waiver_is_not_a_reviewer_and_light_lots_can_merge(self):
        from ameesh import lot_class
        item, marks = self.marks()
        self.assertEqual(stagnation.waiting(item, marks, review_class=lot_class.LIGHT)["what"],
                         "merge")
        item.update(reviewer=work.REVIEWER_NONE, reviewers=[work.REVIEWER_NONE])
        marks = progress._work_item_milestones(item, [], [], [])
        self.assertEqual(marks["reviewers"], [])
        self.assertEqual(marks["pending_reviewers"], [])
        self.assertFalse(marks["verdict_required"])
        self.assertEqual(stagnation.waiting(item, marks)["what"], "merge")
        self.assertIsNone(progress.build_lot(dict(item, id=42), [], [])["reviewer"])


class MultipleReviewersTest(PgTestCase):
    def setUp(self):
        super().setUp()
        for name in ("author", "first", "second", "chief"):
            self.register(name, "codex", cwd=self.tmp)
        self.lot = work.add(self.db, title="two reviews", assignee="author", app="ameesh")
        work.move(self.db, self.lot["id"], "build")
        work.move(self.db, self.lot["id"], "qa")

    def designate(self, name, **kwargs):
        return storage.of(self.db).work.designate_reviewer(self.lot["id"], name,
                                                          actor="chief", **kwargs)

    def test_accumulation_cli_removal_and_assignment(self):
        work.milestone(self.db, self.lot["id"], "frozen", sha="a" * 40, actor="author")
        self.designate("first")
        self.designate("second")
        self.designate("second")
        row = work.get(self.db, self.lot["id"])
        self.assertEqual(row["reviewers"], ["first", "second"])
        shown = self.mesh("work", "show", str(self.lot["id"]))
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn("reviewer : first, second", shown.stdout)
        self.assertIn("verdict by first, second", shown.stdout)
        proc = self.mesh("work", "reviewer", str(self.lot["id"]), "first", "--remove",
                         "--actor", "chief", env=self.env(AMEESH_ALERT_ORCHESTRATORS="chief"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(work.get(self.db, self.lot["id"])["reviewers"], ["second"])
        self.assertTrue(any("reviewer first removed" in e["note"]
                            for e in work.events(self.db, self.lot["id"])))
        # Assignment uses the legacy SQL path; the trigger removes the new
        # author atomically and keeps all unrelated designations.
        self.designate("first")
        work.assign(self.db, self.lot["id"], "second", actor="chief", cfg=self.cfg)
        row = work.get(self.db, self.lot["id"])
        self.assertEqual(row["reviewers"], ["first"])
        self.assertEqual(row["reviewer"], "first")

    def test_direct_sql_assignment_journals_the_cleared_reviewer(self):
        self.designate("first")
        self.designate("second")
        self.db.execute("UPDATE work_items SET assignee = 'first' WHERE id = %s",
                        (self.lot["id"],))
        self.assertEqual(work.get(self.db, self.lot["id"])["reviewers"], ["second"])
        notes = [e["note"] for e in work.events(self.db, self.lot["id"])]
        self.assertIn("reviewer first cleared (they now hold the lot)", notes)

    def test_redesignation_via_api_moves_the_existing_name_last(self):
        for name in ("first", "second", "first"):
            self.designate(name)
        row = work.get(self.db, self.lot["id"])
        self.assertEqual(row["reviewers"], ["second", "first"])
        self.assertEqual(row["reviewer"], "first")

    def test_legacy_writer_accumulates_and_cannot_reintroduce_author(self):
        for name in ("first", "second", "first"):
            self.db.execute("UPDATE work_items SET reviewer = %s WHERE id = %s",
                            (name, self.lot["id"]))
        # #2204/N2 : re-désigner un nom déjà présent le remet en dernier.
        row = work.get(self.db, self.lot["id"])
        self.assertEqual(row["reviewers"], ["second", "first"])
        self.assertEqual(row["reviewer"], "first")
        self.db.execute("UPDATE work_items SET assignee = 'first' WHERE id = %s",
                        (self.lot["id"],))
        self.assertEqual(work.get(self.db, self.lot["id"])["reviewers"], ["second"])
        self.assertIsNone(self.designate("first"))

    def test_global_removal_and_waiver_replace_all_designations(self):
        for flag in ("--remove", "--no-verdict"):
            with self.subTest(flag=flag):
                self.designate("first")
                self.designate("second")
                proc = self.mesh("work", "reviewer", str(self.lot["id"]), flag,
                                 "--actor", "chief",
                                 env=self.env(AMEESH_ALERT_ORCHESTRATORS="chief"))
                self.assertEqual(proc.returncode, 0, proc.stderr)
                row = work.get(self.db, self.lot["id"])
                self.assertEqual(row["reviewers"],
                                 [work.REVIEWER_NONE] if flag == "--no-verdict" else [])
                marks = progress._work_item_milestones(row, [], [], [])
                self.assertEqual(marks["pending_reviewers"], [])
                self.assertEqual(marks["verdict_required"], flag != "--no-verdict")
                # A fresh designation clears the waiver, and accumulates again.
                self.designate("first")
                self.designate("second")
                self.assertEqual(work.get(self.db, self.lot["id"])["reviewers"],
                                 ["first", "second"])

    def test_legacy_waiver_is_replaced_by_a_new_designation(self):
        self.designate("first")
        self.designate("second")
        self.db.execute("UPDATE work_items SET reviewer = %s WHERE id = %s",
                        (work.REVIEWER_NONE, self.lot["id"]))
        self.assertEqual(work.get(self.db, self.lot["id"])["reviewers"], [work.REVIEWER_NONE])
        self.db.execute("UPDATE work_items SET reviewer = 'first' WHERE id = %s",
                        (self.lot["id"],))
        self.assertEqual(work.get(self.db, self.lot["id"])["reviewers"], ["first"])

    def test_delegation_and_return_preserve_other_reviewers(self):
        self.designate("first")
        self.designate("second")
        delegated = storage.of(self.db).work.delegate(
            self.lot["id"], "first", current="author", delegated_by="author",
            within_s=1800, note="delegate", actor="chief")
        self.assertEqual(delegated["item"]["reviewers"], ["second"])
        self.designate("author")
        returned = work.expire_delegations(self.db, now=delegated["delegation"]["due_ts"] + 1)
        self.assertEqual(returned[0]["outcome"], "rendue")
        self.assertEqual(work.get(self.db, self.lot["id"])["reviewers"], ["second"])

    def test_removal_keeps_the_designation_authority_check(self):
        self.designate("first")
        proc = self.mesh("work", "reviewer", str(self.lot["id"]), "first", "--remove",
                         "--actor", "author")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("designation refused", proc.stderr)
        self.assertEqual(work.get(self.db, self.lot["id"])["reviewers"], ["first"])

    def test_concurrent_designations_keep_both_names(self):
        barrier = threading.Barrier(2)

        def add(name):
            db = self.connect()
            try:
                barrier.wait(timeout=10)
                return storage.of(db).work.designate_reviewer(self.lot["id"], name,
                                                              actor="chief")
            finally:
                db.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            rows = list(pool.map(add, ["first", "second"]))
        self.assertTrue(all(rows))
        self.assertEqual(set(work.get(self.db, self.lot["id"])["reviewers"]), {"first", "second"})

    def test_pre_migration_reads_backfill_and_live_connection(self):
        from ameesh import db as db_mod
        migration = Path(REPO, "src/ameesh/migrations/0456_work_item_reviewers.sql").read_text()
        audit = Path(REPO, "src/ameesh/migrations/0462_reviewer_audit.sql").read_text()
        spelling = Path(REPO, "src/ameesh/migrations/0516_agent_rename.sql").read_text()
        # 0529 also depends on reviewers. Suspend only its seat trigger while
        # simulating the pre-0456 schema, then restore it after the column.
        self.addCleanup(
            self.db.execute,
            "CREATE TRIGGER ameesh_review_seats_guard AFTER INSERT OR UPDATE OF reviewers, reviewer "
            "ON work_items FOR EACH ROW EXECUTE FUNCTION ameesh_review_seats_guard()")
        self.db.execute("DROP TRIGGER ameesh_review_seats_guard ON work_items")
        self.db.execute("DROP TRIGGER work_item_reviewers_sync ON work_items")
        self.db.execute("DROP TRIGGER IF EXISTS work_item_reviewers_audit ON work_items")
        # 0516 : le trigger d'orthographe des noms d'agents dépend de la colonne.
        self.db.execute("DROP TRIGGER IF EXISTS ameesh_agent_spelling ON work_items")
        self.db.execute("ALTER TABLE work_items DROP COLUMN reviewers")
        # LIFO : 0456 d'abord, puis 0462 qui rétablit le trigger d'audit,
        # puis 0516 qui rétablit celui des noms d'agents.
        self.addCleanup(self.db.script, spelling)
        self.addCleanup(self.db.script, audit)
        self.addCleanup(self.db.script, migration)
        self.db.__dict__.pop("_has_reviewers", None)
        self.db.execute("UPDATE work_items SET reviewer = 'first' WHERE id = %s",
                        (self.lot["id"],))
        row = work.get(self.db, self.lot["id"])
        self.assertIsNone(row["reviewers"])
        self.assertEqual(row["reviewer"], "first")
        with self.assertRaisesRegex(db_mod.DbError, "0456"):
            self.designate("second")
        work.move(self.db, self.lot["id"], "build")
        with self.assertRaisesRegex(work.WorkError, "0456"):
            work.move(self.db, self.lot["id"], "qa", reviewer="second", actor="human:chief")
        self.assertEqual(work.get(self.db, self.lot["id"])["state"], "build")
        self.db.execute(migration)
        work.move(self.db, self.lot["id"], "qa")
        # A negative catalogue lookup was not cached by the live connection.
        self.assertEqual(self.designate("second")["reviewers"], ["first", "second"])
        self.designate("first", remove=True)
        self.db.execute(migration)
        self.assertEqual(work.get(self.db, self.lot["id"])["reviewers"], ["second"])

    # -- #2084/N1-N2 : journaliser tout changement, jamais un retrait sans effet

    def notes(self):
        return [e["note"] for e in work.events(self.db, self.lot["id"])]

    def test_assigning_a_missing_lot_is_a_noop(self):
        self.assertIsNone(storage.of(self.db).work.assign(
            -1, "first", current=None, note="missing lot", actor="chief"))

    def test_removing_an_undesignated_name_writes_no_journal_line(self):
        self.designate("first")
        before = self.notes()
        updated = work.get(self.db, self.lot["id"])["updated_ts"]
        proc = self.mesh("work", "reviewer", str(self.lot["id"]), "ghost", "--remove",
                         "--actor", "chief",
                         env=self.env(AMEESH_ALERT_ORCHESTRATORS="chief"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("was not designated: nothing removed", proc.stdout)
        self.assertEqual(self.notes(), before)
        row = work.get(self.db, self.lot["id"])
        self.assertEqual(row["reviewers"], ["first"])
        self.assertEqual(row["updated_ts"], updated)

    def test_removing_all_without_a_designation_writes_no_journal_line(self):
        before = self.notes()
        updated = work.get(self.db, self.lot["id"])["updated_ts"]
        proc = self.mesh("work", "reviewer", str(self.lot["id"]), "--remove",
                         "--actor", "chief",
                         env=self.env(AMEESH_ALERT_ORCHESTRATORS="chief"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("no reviewer designation to remove", proc.stdout)
        self.assertEqual(self.notes(), before)
        self.assertEqual(work.get(self.db, self.lot["id"])["updated_ts"], updated)

    def test_assignment_to_a_non_last_reviewer_names_the_removal(self):
        self.designate("first")
        self.designate("second")
        work.assign(self.db, self.lot["id"], "first", actor="chief", cfg=self.cfg)
        row = work.get(self.db, self.lot["id"])
        self.assertEqual(row["reviewers"], ["second"])
        notes = self.notes()
        self.assertIn("reviewer first cleared (they now hold the lot)", notes)
        assigned = [n for n in notes if n.startswith("assigned to first")]
        self.assertTrue(assigned, notes)
        self.assertNotIn("cleared", assigned[0])

    def test_delegation_to_a_non_last_reviewer_names_the_removal(self):
        self.designate("first")
        self.designate("second")
        out = work.delegate(self.db, self.lot["id"], "first", within=1800,
                            actor="author", cfg=self.cfg)
        self.assertEqual(out["item"]["reviewers"], ["second"])
        self.assertIn("reviewer first cleared (they now hold the lot)", self.notes())

    def test_delegation_return_to_a_non_last_reviewer_names_the_removal(self):
        self.designate("first")
        self.designate("second")
        out = work.delegate(self.db, self.lot["id"], "first", within=1800,
                            actor="author", cfg=self.cfg)
        self.designate("author")
        self.assertEqual(work.get(self.db, self.lot["id"])["reviewers"],
                         ["second", "author"])
        done = work.expire_delegations(self.db,
                                       now=out["delegation"]["due_ts"] + 1)
        self.assertEqual([d["outcome"] for d in done], ["rendue"])
        self.assertEqual(work.get(self.db, self.lot["id"])["reviewers"], ["second"])
        self.assertIn("reviewer author cleared (they now hold the lot)", self.notes())

    # -- #2346 : le drapeau d'audit ne couvre que l'UPDATE qui l'a posé

    def cleared_notes(self):
        return [e for e in work.events(self.db, self.lot["id"])
                if " cleared " in e["note"]]

    def assert_trigger_cleared(self, name):
        cleared = [e for e in self.cleared_notes()
                   if e["note"] == "reviewer %s cleared (they now hold the lot)" % name]
        self.assertEqual([e["actor"] for e in cleared], ["db:reviewers_sync"],
                         [e["note"] for e in self.cleared_notes()])

    def test_assign_in_a_calling_transaction_does_not_mask_a_direct_removal(self):
        self.designate("first")
        self.designate("second")
        with self.db.transaction() as tx:
            storage.of(tx).work.assign(self.lot["id"], "chief", current="author",
                                       note="take", actor="chief")
            # Retrait SQL direct, plus loin dans la MÊME transaction : le
            # drapeau d'audit du chemin Python ne doit plus le taire.
            tx.execute("UPDATE work_items SET assignee = 'first' WHERE id = %s",
                       (self.lot["id"],))
        self.assert_trigger_cleared("first")

    def test_delegate_in_a_calling_transaction_does_not_mask_a_direct_removal(self):
        self.designate("first")
        self.designate("second")
        with self.db.transaction() as tx:
            storage.of(tx).work.delegate(self.lot["id"], "first", current="author",
                                         delegated_by="author", within_s=1800,
                                         note="delegated", actor="author")
            tx.execute("UPDATE work_items SET assignee = 'second' WHERE id = %s",
                       (self.lot["id"],))
        self.assert_trigger_cleared("second")

    def test_expire_in_a_calling_transaction_does_not_mask_a_direct_removal(self):
        self.designate("first")
        self.designate("second")
        out = work.delegate(self.db, self.lot["id"], "first", within=1800,
                            actor="author", cfg=self.cfg)
        self.designate("author")
        with self.db.transaction() as tx:
            done = storage.of(tx).work.resolve_delegation(
                out["delegation"]["delegation_id"],
                now_ts=out["delegation"]["due_ts"] + 1,
                actor="runner", describe=None)
            self.assertEqual(done["outcome"], "rendue")
            tx.execute("UPDATE work_items SET assignee = 'second' WHERE id = %s",
                       (self.lot["id"],))
        self.assert_trigger_cleared("second")

    # -- #2346 : un homonyme agent ne vaut pas l'humain `human:<nom>`

    def test_snapshot_does_not_credit_a_human_verdict_to_an_agent(self):
        sha = "a" * 40
        self.designate("first")
        work.milestone(self.db, self.lot["id"], "frozen", sha=sha, actor="author")
        work.milestone(self.db, self.lot["id"], "verdict", sha=sha,
                       actor="human:first", verdict="ok")
        view = progress.snapshot(self.db, self.cfg, since="24h", gantt=False)
        lot = next(row for row in view["lots"] if row["id"] == self.lot["id"])
        self.assertEqual(lot["pending_reviewers"], ["first"])
        self.assertEqual(lot["state"], "review")

    def test_snapshot_does_not_credit_an_agent_verdict_to_a_human(self):
        sha = "b" * 40
        human = work.add(self.db, title="human review", assignee="author", app="ameesh")
        work.move(self.db, human["id"], "build")
        work.move(self.db, human["id"], "qa")
        storage.of(self.db).work.designate_reviewer(human["id"], "human:first",
                                                    actor="chief")
        work.milestone(self.db, human["id"], "frozen", sha=sha, actor="author")
        work.milestone(self.db, human["id"], "verdict", sha=sha,
                       actor="first", verdict="ok")
        view = progress.snapshot(self.db, self.cfg, since="24h", gantt=False)
        lot = next(row for row in view["lots"] if row["id"] == human["id"])
        self.assertEqual(lot["pending_reviewers"], ["human:first"])
        self.assertEqual(lot["state"], "review")
