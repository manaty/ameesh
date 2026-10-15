# SPDX-License-Identifier: AGPL-3.0-only
"""Queue changes and exclusion races through real admission storage."""
import dataclasses
import os
import subprocess
import types
from unittest import mock

from ameesh import storage, train, train_admissions, train_history, work
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

    def test_real_git_history_and_sql_freezes_cover_correction_then_refuse_new_freeze(self):
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
        base, first, head = commit("base"), commit("author"), commit("correction")
        item = self.entry["work_item_id"]
        freeze = self.ops.add_milestone(item, "frozen", sha=head, actor="fixture", verdict=None, note="")
        entry = self.ops.queue_add(item, target="main", frozen_sha=head, note="", actor="fixture",
                                  source=self.admissions.source, frozen_id=freeze["id"])
        receipts = {"commit:" + sha: dataclasses.asdict(author(sha)) for sha in (first, head)}
        for reader, family in (("r1", "claude"), ("r2", "deepseek")):
            mark = self.ops.add_milestone(item, "verdict", sha=head, actor=reader, verdict="ok", note="")
            _, receipt = verdict(mark["id"], reader, family, commit=head)
            receipt = dataclasses.replace(receipt, milestone={
                key: mark[key] for key in ("id", "actor", "at_ts", "sha", "verdict")})
            receipts[receipt.subject] = dataclasses.asdict(receipt)
        provider = types.SimpleNamespace(ProvenanceError=RuntimeError,
            receipt_for_commit=lambda db, sha: receipts.get("commit:" + sha),
            receipt_for_milestone=lambda db, mid: receipts.get("milestone:%d" % mid))
        self.admissions.history = train_history.Recorded(self.db, self.tmp, provider=provider)
        proof = self.admissions.qualify([entry], base, "packet")
        coverage = proof["members"][0]["history"]["reviews"]["coverage"]
        self.assertEqual(set(coverage), {first, head})
        self.assertTrue(all(len(readers) == 2 for readers in coverage.values()))
        self.ops.add_milestone(item, "frozen", sha=head, actor="fixture", verdict=None, note="new gel")
        with self.assertRaisesRegex(train.TrainError, "latest exact freeze"):
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
