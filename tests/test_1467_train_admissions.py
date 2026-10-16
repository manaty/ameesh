# SPDX-License-Identifier: AGPL-3.0-only
"""Queue changes and exclusion races through real admission storage."""
import dataclasses
import json
import os
import shutil
import subprocess
import threading
import types
from unittest import mock

from ameesh import (resolutions, storage, train, train_admissions, train_candidates,
                    train_git, train_history, train_policy, work)
from .support import PgTestCase
from .test_1467_train_review import author, verdict

BASE, HEAD = "a" * 40, "b" * 40


class TrainAdmissionsTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.project = types.SimpleNamespace(name="admission-test", repo=self.tmp, target="main")
        self.ops = storage.of(self.db).work
        self.history = mock.Mock(db=self.db, path=self.tmp)
        self.history.qualify.side_effect = lambda row, base, **kw: dict(
            head=row["frozen_sha"], required=kw["required"])
        self.requirement = mock.Mock(side_effect=lambda item, row, base, path:
            train_admissions.Requirement(row["work_item_id"], base, row["frozen_sha"],
                                         "sensitive", 2, "c" * 40))
        self.admissions = train_admissions.Admissions(
            self.db, self.project, self.history, policy=self.requirement)
        self.entry = self.enqueue()

    def enqueue(self, *, source=None, target="main"):
        item = work.add(self.db, title="train member", app=self.project.name, source="fixture",
                        actor="fixture", branch="lot/admission", branch_target=target)
        freeze = self.ops.add_milestone(item["id"], "frozen", sha=HEAD, actor="fixture",
                                         verdict=None, note="")
        return self.ops.queue_add(item["id"], target=target, frozen_sha=HEAD, note="", actor="fixture",
            source=self.admissions.source if source is None else source, frozen_id=freeze["id"])

    def qualify(self, mode="packet"):
        return self.admissions.qualify([self.entry], BASE, mode)

    def lease(self):
        key = self.project.name + ":main"
        epoch = self.ops.land_lock(key, "train:fixture", 60)
        return key, "train:fixture", epoch

    def test_packet_binds_versioned_requirement_and_reads_no_individual_ci(self):
        self.admissions.targeted_ci = mock.Mock(side_effect=AssertionError("unexpected lot CI"))
        proof = self.qualify()
        member = proof["members"][0]
        self.assertEqual(member["policy"]["reviews"], 2)
        self.assertEqual(member["policy"]["policy_commit"], "c" * 40)
        self.history.qualify.assert_called_once_with(self.entry, BASE, required=2)

    def test_other_source_and_inactive_queue_rows_do_not_enter(self):
        foreign = self.enqueue(source="github.com/foreign/repository")
        ejected = self.enqueue()
        self.ops.queue_update(ejected["work_item_id"], state="ejected")
        self.assertEqual([r["work_item_id"] for r in self.admissions.entries()],
                         [self.entry["work_item_id"]])
        self.assertNotEqual(foreign["work_item_id"], self.entry["work_item_id"])

    def test_same_sha_requeue_invalidates_previous_admission(self):
        self.ops.queue_add(self.entry["work_item_id"], target="main", frozen_sha=HEAD,
                           note="new generation", actor="fixture", source=self.admissions.source)
        with self.assertRaisesRegex(train.TrainError, "requeued"):
            self.qualify()

    def test_waiting_lot_and_changed_project_target_or_branch_refuse(self):
        item = self.entry["work_item_id"]
        for field, value in (("state", "blocked"), ("state", "waiting_human"),
                             ("app", "other"), ("branch", "lot/other"),
                             ("branch_target", "develop")):
            original = self.ops.get(item)[field]
            # Identifiers come from this fixed fixture list, never from input.
            self.db.execute("UPDATE work_items SET " + field + " = %s WHERE id = %s", (value, item))
            with self.subTest(field=field, value=value), self.assertRaises(train.TrainError):
                self.qualify()
            self.db.execute("UPDATE work_items SET " + field + " = %s WHERE id = %s", (original, item))

    def test_release_target_is_refused_even_with_a_frozen_queued_entry(self):
        release = self.enqueue(target="release/1.0")
        with self.assertRaisesRegex(train.TrainError, "release lots"):
            self.admissions.qualify([release], BASE, "packet")
        self.requirement.assert_not_called()

    def test_policy_must_name_exact_subject_and_version_with_valid_review_count(self):
        valid = self.requirement(None, self.entry, BASE, self.tmp)
        for fields in (dict(lot=999999), dict(base=HEAD), dict(head=BASE), dict(policy_commit=""),
                       dict(risk="release"), dict(risk="sensitive", reviews=0), dict(reviews=True)):
            self.requirement.side_effect = None
            self.requirement.return_value = dataclasses.replace(valid, **fields)
            with self.subTest(fields=fields), self.assertRaises(train.TrainError):
                self.qualify()
        self.history.qualify.assert_not_called()

    def test_unknown_history_stops_admission_after_policy(self):
        self.history.qualify.side_effect = train.TrainError("unknown historical author")
        with self.assertRaisesRegex(train.TrainError, "unknown historical author"):
            self.qualify()

    def recorded_admission(self, *, light=False, reviewed=True):
        env = dict(os.environ, GIT_AUTHOR_NAME="fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid",
                   GIT_COMMITTER_NAME="fixture", GIT_COMMITTER_EMAIL="fixture@example.invalid")
        def git(*args):
            return subprocess.check_output(["git", "-C", self.tmp, *args], env=env,
                                           text=True, stderr=subprocess.PIPE).strip()
        git("init", "-q")
        def commit(content):
            with open(os.path.join(self.tmp, "content"), "w") as fh:
                fh.write(content)
            git("add", "content")
            git("commit", "-qm", content)
            return git("rev-parse", "HEAD")
        with open(os.path.join(self.tmp, "federation.yaml"), "w") as fh:
            json.dump(dict(review_policies=dict(
                risk_classes=dict(projects={self.project.name: dict(
                    default="light" if light else "sensitive", rules=[])}),
                train_reviews=dict(schema="ameesh-train-reviews-policy/1", projects={
                    self.project.name: dict(rules=[] if light else [
                        dict(paths=["content"], reviews=2)])}))), fh)
        git("add", "federation.yaml")
        base, first, head = commit("base"), commit("author"), commit("correction")
        self.admissions.policy = train_policy.Versioned(self.tmp, base, self.project.name)
        item = self.entry["work_item_id"]
        freeze = self.ops.add_milestone(item, "frozen", sha=head, actor="fixture", verdict=None, note="")
        entry = self.ops.queue_add(item, target="main", frozen_sha=head, note="", actor="fixture",
                                  source=self.admissions.source, frozen_id=freeze["id"])
        receipts = {"commit:" + sha: dataclasses.asdict(author(sha)) for sha in (first, head)}
        for reader, family in ((("r1", "claude"), ("r2", "deepseek")) if reviewed else ()):
            mark = self.ops.add_milestone(item, "verdict", sha=head, actor=reader, verdict="ok", note="")
            _, receipt = verdict(mark["id"], reader, family, commit=head)
            receipt = dataclasses.replace(receipt, milestone={
                key: mark[key] for key in ("id", "actor", "at_ts", "sha", "verdict")})
            receipts[receipt.subject] = dataclasses.asdict(receipt)
        provider = types.SimpleNamespace(ProvenanceError=RuntimeError,
            receipt_for_commit=lambda db, sha: receipts.get("commit:" + sha),
            receipt_for_milestone=lambda db, mid: receipts.get("milestone:%d" % mid))
        self.admissions.history = train_history.Recorded(self.db, self.tmp, provider=provider)
        return base, first, head, entry

    def test_real_git_history_and_sql_freezes_cover_correction_then_refuse_new_freeze(self):
        base, first, head, entry = self.recorded_admission()
        proof = self.admissions.qualify([entry], base, "packet")
        self.assertEqual(proof["members"][0]["policy"]["policy_commit"], base)
        coverage = proof["members"][0]["history"]["reviews"]["coverage"]
        self.assertEqual(set(coverage), {first, head})
        self.assertTrue(all(len(readers) == 2 for readers in coverage.values()))
        self.ops.add_milestone(entry["work_item_id"], "frozen", sha=head,
                               actor="fixture", verdict=None, note="new gel")
        with self.assertRaisesRegex(train.TrainError, "latest exact freeze"):
            self.admissions.qualify([entry], base, "packet")

    def enrollment_fixture(self):
        base, first, head, old_entry = self.recorded_admission()
        item = old_entry["work_item_id"]
        self.db.execute("DELETE FROM merge_queue WHERE work_item_id = %s", (item,))
        self.db.execute("UPDATE work_items SET state = 'qa' WHERE id = %s", (item,))
        for name, sha in (("main", base), ("lot/admission", head)):
            subprocess.run(["git", "-C", self.tmp, "branch", name, sha],
                           check=True, capture_output=True)
        clone = resolutions.Replayer(self.tmp, os.path.join(self.tmp, "private.clone"))
        repository = train_git.Repository(self.project, clone)
        history = train_history.Recorded(self.db, clone.path, provider=self.admissions.history.provider)
        self.admissions.history = history
        preparation = train_candidates.Preparation(self.db, self.project,
            repository=repository, history=history, policy=self.admissions.policy)
        freeze = next(mark for mark in self.ops.milestones(item, None)
                      if mark["id"] == old_entry["frozen_id"])
        candidate = dict(work_item_id=item, branch="lot/admission", target="main", causes=[],
                         candidate=dict(sha=head[:12], origin="frozen", declared=freeze))
        return base, first, head, item, candidate, preparation, old_entry

    def enrollment(self, preparation):
        # The initial history of this disposable schema is a known fixture.
        # No production legacy-history reader is selected by the product.
        return train_candidates.Enrollment(preparation, self.admissions,
                                          initial=lambda db, **context: True)

    def test_qa_preparation_precedes_real_generation_and_keeps_packet_ci_pending(self):
        base, first, head, item, candidate, preparation, old_entry = self.enrollment_fixture()
        marks_before = self.ops.milestones(item, None)
        subject = preparation.prepare(candidate, base)["subject"]
        self.assertEqual(self.ops.queue(None), [])
        self.assertNotIn("generation", subject)
        enrollment = self.enrollment(preparation)
        result = enrollment.enroll(candidate, base, actor="fixture")
        entry = result["entry"]
        self.assertGreater(entry["generation"], old_entry["generation"])
        proof = result["proof"]["members"][0]
        self.assertEqual(set(proof["history"]["reviews"]["coverage"]), {first, head})
        self.assertNotIn("targeted_ci", proof)
        self.assertEqual(self.ops.milestones(item, None), marks_before)
        replay = enrollment.enroll(candidate, base, actor="restarted-fixture")
        self.assertEqual(replay, result)

    def test_enrollment_rechecks_fresh_generation_and_reviews_after_allocation(self):
        base, _, _, item, candidate, preparation, _ = self.enrollment_fixture()
        enrollment = self.enrollment(preparation)
        other = self.connect()
        self.addCleanup(other.close)
        actual = preparation.ops.queue_add
        def withdrawn(*args, **kwargs):
            row = actual(*args, **kwargs)
            storage.of(other).work.queue_remove(item, note="withdrawn before reread", actor="fixture")
            return row
        with mock.patch.object(preparation.ops, "queue_add", side_effect=withdrawn):
            with self.assertRaisesRegex(train.TrainError, "admission withdrawn"):
                enrollment.enroll(candidate, base, actor="fixture")
        self.assertEqual(self.ops.queue(None), [])
        with self.assertRaisesRegex(train.TrainError, "changed/withdrawn"):
            enrollment.enroll(candidate, base, actor="fixture")

    def test_enrollment_rechecks_a_blocking_vote_committed_after_allocation(self):
        base, _, head, item, candidate, preparation, _ = self.enrollment_fixture()
        other = self.connect()
        self.addCleanup(other.close)
        actual = preparation.ops.queue_add
        def blocked(*args, **kwargs):
            row = actual(*args, **kwargs)
            storage.of(other).work.add_milestone(item, "verdict", sha=head, actor="blocked-reader",
                                                 verdict="blocked", note="fresh blockage")
            return row
        with mock.patch.object(preparation.ops, "queue_add", side_effect=blocked):
            with self.assertRaisesRegex(train.TrainError, "unresolved blocking"):
                self.enrollment(preparation).enroll(
                    candidate, base, actor="fixture")

    def test_enrollment_refuses_refreeze_between_preparation_and_allocation(self):
        base, _, head, item, candidate, preparation, _ = self.enrollment_fixture()
        other = self.connect()
        self.addCleanup(other.close)
        actual = preparation.ops.queue_add
        def refrozen(*args, **kwargs):
            storage.of(other).work.add_milestone(item, "frozen", sha=head, actor="fixture",
                                                 verdict=None, note="new freeze before write")
            return actual(*args, **kwargs)
        with mock.patch.object(preparation.ops, "queue_add", side_effect=refrozen):
            with self.assertRaisesRegex(train.TrainError, "subject, freeze or queue changed/withdrawn"):
                self.enrollment(preparation).enroll(
                    candidate, base, actor="fixture")
        self.assertEqual(self.ops.queue(None), [])

    def test_enrollment_names_a_refused_candidate_and_continues_independent_candidates(self):
        base, _, _, item, candidate, preparation, _ = self.enrollment_fixture()
        result = self.enrollment(preparation).enroll_many(
            [dict(candidate, work_item_id=999), candidate], base, actor="fixture")
        self.assertEqual([r["entry"]["work_item_id"] for r in result["admitted"]], [item])
        self.assertEqual([r["work_item_id"] for r in result["deferred"]], [999])
        self.assertIn("QA branch", result["deferred"][0]["reason"])

    def test_ambiguous_legacy_history_refuses_first_insert_but_known_replay_needs_no_bootstrap(self):
        base, _, _, item, candidate, preparation, _ = self.enrollment_fixture()
        unknown = train_candidates.Enrollment(preparation, self.admissions)
        with self.assertRaisesRegex(train.TrainError, "queue-history-unknown"):
            unknown.enroll(candidate, base, actor="fixture")
        self.assertEqual(self.ops.queue(None), [])
        admitted = self.enrollment(preparation).enroll(candidate, base, actor="fixture")
        self.assertEqual(unknown.enroll(candidate, base, actor="restart"), admitted)

    def test_recorded_enrollment_survives_clone_loss_and_fresh_connection_then_reports_withdrawal(self):
        base, _, _, item, candidate, preparation, _ = self.enrollment_fixture()
        admitted = self.enrollment(preparation).enroll(candidate, base, actor="fixture")
        before = work.events(self.db, item)
        sequence = self.db.query("SELECT last_value FROM merge_queue_generation_seq")
        clone_path = preparation.repository.clone.path
        shutil.rmtree(clone_path)
        other = self.connect()
        self.addCleanup(other.close)
        repository = train_git.Repository(self.project, resolutions.Replayer(self.tmp, clone_path))
        history = train_history.Recorded(other, clone_path, provider=preparation.history.provider)
        admissions = train_admissions.Admissions(other, self.project, history,
            policy=self.admissions.policy,
            targeted_ci=mock.Mock(side_effect=AssertionError("unexpected individual CI")))
        fresh = train_candidates.Preparation(other, self.project, repository=repository,
                                             history=history, policy=admissions.policy)
        unknown = mock.Mock(return_value=None)
        restarted = train_candidates.Enrollment(fresh, admissions, initial=unknown)
        result = restarted.enroll_many([candidate], base, actor="restarted-fixture")
        self.assertEqual(result, dict(admitted=[admitted], deferred=[]))
        unknown.assert_called_once_with(other, subject=preparation.prepare(candidate, base)["subject"])
        self.assertEqual(work.events(self.db, item), before)
        self.assertEqual(self.db.query("SELECT last_value FROM merge_queue_generation_seq"), sequence)
        self.ops.queue_remove(item, note="withdrawn after restart", actor="fixture")
        refused = restarted.enroll_many([candidate], base, actor="restarted-fixture")
        self.assertEqual(refused["admitted"], [])
        self.assertEqual([r["work_item_id"] for r in refused["deferred"]], [item])
        self.assertIn("changed/withdrawn", refused["deferred"][0]["reason"])
        self.assertEqual(self.ops.queue(None), [])

    def test_light_admission_with_no_verdict_uses_versioned_zero_review_policy(self):
        # #1734: a LIGHT lot must not disappear behind a positive-vote filter.
        base, first, head, entry = self.recorded_admission(light=True, reviewed=False)
        self.admissions.targeted_ci = mock.Mock(side_effect=AssertionError("unexpected lot CI"))
        self.assertIn(entry, self.admissions.entries())
        proof = self.admissions.qualify([entry], base, "packet")["members"][0]
        self.assertEqual((proof["policy"]["risk"], proof["policy"]["reviews"]), ("light", 0))
        self.assertEqual(proof["history"]["reviews"]["coverage"], {first: [], head: []})
        self.assertEqual(proof["history"]["reviews"]["reviews"], [])

    def test_sensitive_admission_with_no_verdict_still_refuses(self):
        base, _, _, entry = self.recorded_admission(reviewed=False)
        with self.assertRaisesRegex(train.TrainError, "missing independent final-head reviews"):
            self.admissions.qualify([entry], base, "packet")

    def test_light_admission_still_preserves_an_explicit_blockage(self):
        base, _, head, entry = self.recorded_admission(light=True, reviewed=False)
        self.ops.add_milestone(entry["work_item_id"], "verdict", sha=head,
                               actor="blocked-reader", verdict="blocked", note="")
        with self.assertRaisesRegex(train.TrainError, "unresolved blocking.*blocked-reader"):
            self.admissions.qualify([entry], base, "packet")

    def test_dependency_read_keeps_open_prerequisite_outside_packet(self):
        prereq = self.enqueue()
        storage.of(self.db).work_dependencies.add(self.entry["work_item_id"],
                                                  prereq["work_item_id"], actor="fixture")
        edges = self.admissions.dependencies([self.entry])
        self.assertEqual(train.ordered([self.entry], edges), [])
        self.db.execute("UPDATE work_items SET state = 'merged' WHERE id = %s",
                        (prereq["work_item_id"],))
        edges = self.admissions.dependencies([self.entry])
        self.assertEqual(len(edges), 1)
        self.assertEqual(train.ordered([self.entry], edges), [self.entry])

    def test_targeted_mode_requires_exact_runs_and_packet_does_not_inherit_them(self):
        for ci in (None, dict(head=HEAD, status="green", complete=True, runs=[]),
                   dict(head=BASE, status="green", complete=True, runs=[1]),
                   dict(head=HEAD, status="pending", complete=False, runs=[1])):
            self.admissions.targeted_ci = lambda row: ci
            with self.subTest(ci=ci), self.assertRaisesRegex(train.TrainError, "targeted lot CI"):
                self.qualify("targeted")
        self.admissions.targeted_ci = lambda row: dict(head=HEAD, status="green", complete=True, runs=[1])
        self.assertEqual(self.qualify("targeted")["members"][0]["targeted_ci"]["runs"], [1])
        self.assertNotIn("targeted_ci", self.qualify()["members"][0])

    def test_exclusion_is_guarded_by_live_lease_and_exact_generation(self):
        lease = self.lease()
        self.admissions.quarantine([self.entry], "isolated complete CI red", lease=lease)
        row = self.ops.queue(None)[0]
        self.assertEqual((row["state"], row["reason"]), ("ejected", "isolated complete CI red"))

    def test_exclusion_cannot_overwrite_requeue_between_read_and_write(self):
        lease = self.lease()
        update = self.ops.queue_update
        def raced(*args, **kw):
            self.ops.queue_add(self.entry["work_item_id"], target="main", frozen_sha=HEAD,
                              note="requeue", actor="fixture", source=self.admissions.source)
            return update(*args, **kw)
        with mock.patch.object(self.admissions.ops, "queue_update", side_effect=raced):
            with self.assertRaisesRegex(train.TrainError, "exclusion refused"):
                self.admissions.quarantine([self.entry], "old diagnosis", lease=lease)
        self.assertEqual(self.ops.queue(None)[0]["state"], "queued")

    def test_expired_or_unrelated_target_lease_cannot_eject(self):
        lease = self.lease()
        with self.assertRaisesRegex(train.TrainError, "target reservation"):
            self.admissions.quarantine([self.entry], "red", lease=("other:main", lease[1], lease[2]))
        self.db.execute("UPDATE merge_locks SET expires_at = clock_timestamp() - interval '1 second'"
                        " WHERE target = %s", (lease[0],))
        with self.assertRaisesRegex(train.TrainError, "exclusion refused"):
            self.admissions.quarantine([self.entry], "red", lease=lease)
        self.assertEqual(self.ops.queue(None)[0]["state"], "queued")


class PacketAllocationTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.ops = storage.of(self.db).work
        self.item = work.add(self.db, title="packet allocation", app="fixture", branch="lot/member",
                             branch_target="main", actor="fixture")["id"]
        self.db.execute("UPDATE work_items SET state = 'qa' WHERE id = %s", (self.item,))
        item = self.ops.get(self.item)
        self.snapshot = {key: item[key] for key in ("app", "branch", "branch_target", "type")}
        self.snapshot["resolved_target"] = "main"
        mark = self.ops.add_milestone(self.item, "frozen", sha=HEAD, actor="fixture", verdict=None, note="")
        self.arguments = dict(target="main", frozen_sha=HEAD, frozen_id=mark["id"], source="fixture-origin",
                              actor="fixture", note="packet fixture", packet=self.snapshot,
                              packet_initial=True)

    def allocate(self, ops=None, **changes):
        return (ops or self.ops).queue_add(self.item, **dict(self.arguments, **changes))

    def test_restart_reuses_exact_generation_without_new_event_or_sequence_allocation(self):
        row = self.allocate()
        before = work.events(self.db, self.item)
        sequence = self.db.query("SELECT last_value FROM merge_queue_generation_seq")
        other = self.connect()
        self.addCleanup(other.close)
        self.assertEqual(self.allocate(storage.of(other).work, actor="restart"), row)
        self.assertEqual(work.events(self.db, self.item), before)
        self.assertEqual(self.db.query("SELECT last_value FROM merge_queue_generation_seq"), sequence)
        for change in (dict(source="another-origin"), dict(target="develop"), dict(frozen_sha=BASE)):
            with self.subTest(change=change):
                self.assertIsNone(self.allocate(**change))
        self.assertEqual(self.ops.queue(None)[0], row)

    def test_existing_generations_are_never_adopted_overwritten_or_resurrected(self):
        existing = self.allocate(packet=None)
        for state in ("queued", "landing", "ejected", "landed"):
            self.ops.queue_update(self.item, state=state)
            with self.subTest(state=state):
                self.assertIsNone(self.allocate())
                self.assertEqual(self.ops.queue(None)[0]["generation"], existing["generation"])
                self.assertEqual(self.ops.queue(None)[0]["state"], state)

    def test_changed_subject_or_absent_expected_freeze_cannot_allocate(self):
        for field, value in (("state", "build"), ("app", "another"), ("branch", "lot/other"),
                             ("branch_target", "develop")):
            old = self.ops.get(self.item)[field]
            self.db.execute("UPDATE work_items SET " + field + " = %s WHERE id = %s", (value, self.item))
            with self.subTest(field=field):
                self.assertIsNone(self.allocate())
            self.db.execute("UPDATE work_items SET " + field + " = %s WHERE id = %s", (old, self.item))
        self.assertIsNone(self.allocate(frozen_id=None))
        self.assertIsNone(self.allocate(packet=dict(self.snapshot, type="release")))
        self.assertIsNone(self.allocate(packet_initial=False))
        self.assertIsNone(self.allocate(target="develop"))
        self.assertEqual(self.ops.queue(None), [])

    def concurrent_change(self, change):
        other = self.connect()
        result, started = {}, threading.Event()
        def allocate():
            started.set()
            try:
                result["row"] = self.allocate()
            except BaseException as exc:
                result["error"] = exc
        writer = threading.Thread(target=allocate)
        try:
            with other.transaction() as tx:
                tx.query("SELECT id FROM work_items WHERE id = %s FOR NO KEY UPDATE", (self.item,))
                change(storage.of(tx).work)
                writer.start()
                self.assertTrue(started.wait(timeout=10))
            writer.join(timeout=30)
            self.assertFalse(writer.is_alive())
            if "error" in result:
                raise result["error"]
            self.assertIn("row", result)
            return result["row"]
        finally:
            other.close()
            if writer.ident is not None:
                writer.join(timeout=30)

    def test_concurrent_same_sha_refreeze_refuses_old_native_freeze(self):
        result = self.concurrent_change(lambda ops: ops.add_milestone(
            self.item, "frozen", sha=HEAD, actor="fixture", verdict=None, note="new native freeze"))
        self.assertIsNone(result)
        self.assertEqual(self.ops.queue(None), [])

    def test_concurrent_initial_enrollments_share_one_generation(self):
        row = self.concurrent_change(lambda ops: self.allocate(ops))
        self.assertEqual(self.ops.queue(None), [row])
        markers = self.ops.events_noted("ameesh-queue-receipt/1 ", 0)
        self.assertEqual(len(markers), 1)

    def test_concurrent_withdrawal_survives_deletion_and_repeated_fresh_attempts(self):
        self.allocate()
        result = self.concurrent_change(lambda ops: ops.queue_remove(self.item, note="withdrawn", actor="fixture"))
        self.assertIsNone(result)
        self.assertIsNone(self.allocate())
        self.assertEqual(self.ops.queue(None), [])

    def test_withdrawal_of_an_individual_entry_also_prevents_automatic_readmission(self):
        self.allocate(packet=None)
        self.ops.queue_remove(self.item, note="individual withdrawal", actor="fixture")
        self.assertIsNone(self.allocate())
        # The existing explicit individual gesture remains available.
        self.assertIsNotNone(self.allocate(packet=None))

    def test_rollback_discards_both_allocation_and_replay_receipt(self):
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            with self.db.transaction() as tx:
                self.assertIsNotNone(self.allocate(storage.of(tx).work))
                raise RuntimeError("rollback")
        self.assertEqual(self.ops.queue(None), [])
        self.assertFalse(self.ops.events_noted("ameesh-queue-receipt/1 ", 0))
        self.assertIsNotNone(self.allocate())
