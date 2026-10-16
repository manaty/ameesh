# SPDX-License-Identifier: AGPL-3.0-only
"""One passage across real queue, policy Git, clone, leases and promotion.

CI and the provenance provider are explicit fixtures, never live services.
"""
import dataclasses
import json
import os
import shutil
import subprocess
import tempfile
import types
from datetime import datetime
from unittest import mock

from ameesh import (db as db_mod, radar, storage, train, train_ci, train_fire,
                    train_candidates, train_git, train_policy, train_runner, train_transports,
                    train_transport_journal, work)

from .support import PgTestCase
from .test_1467_train_review import author, verdict


class FixtureMechanics(train_policy.Versioned):
    """Explicit #1616 boundary fixture; integration probes use the real loader."""
    schema = "ameesh-mechanics/1"
    ci_packet = "full"
    # This fixture explicitly exercises the older named-fire policy too.
    # The new canon and automatic path are tested with both policy values.
    merge_named_fire = True
    merge_repeat_freeze = False
    merge_on_green_packet = True
    merge_post_merge_red = "forward_or_revert"
    families = ("codex", "claude", "deepseek")
    exclude_author_family = distinct_families = True
    # Independent packet calendar fixture; the release methods must never
    # schedule a train. #1757's real supplier is exercised by the port probe.
    packet_cutoffs = ("11:00", "23:00")
    last_packet = staticmethod(train.cutoff)

    def next_packet(self, now):
        # Explicit fixture calendar; real provider/DST tested by the port probe.
        return now + 1234

    def cutoff(self, now):
        raise AssertionError("release calendar must not schedule a packet")

    next_cutoff = cutoff

    @property
    def review_policy(self):
        return self.policy

    def reviews_for(self, paths):
        risk, count = self._need(paths)
        return types.SimpleNamespace(risk=risk, reviews=count)


class TrainRunnerTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE merge_locks RESTART IDENTITY CASCADE")
        folder = tempfile.TemporaryDirectory(dir=self.tmp)
        self.addCleanup(folder.cleanup)
        self.root = folder.name
        self.publisher, self.host, self.origin = [os.path.join(self.root, name)
                                                for name in ("publisher", "host", "origin.git")]
        os.mkdir(self.publisher)
        self.git(self.publisher, "init", "-q", "-b", "main")
        for key, value in (("user.name", "fixture"), ("user.email", "fixture@example.invalid"),
                           ("core.hooksPath", os.devnull)):
            self.git(self.publisher, "config", key, value)
        self.cfg = types.SimpleNamespace(state_dir=os.path.join(self.root, "state"))
        self.project = radar.Project(name="runner-test", repo=self.host, target="main", github="o/r")
        self.write("federation.yaml", json.dumps(dict(review_policies=dict(
            risk_classes=dict(projects={self.project.name: dict(default="sensitive", rules=[])}),
            train_reviews=dict(schema="ameesh-train-reviews-policy/1", projects={
                self.project.name: dict(rules=[dict(paths=["**"], reviews=2)])})))))
        self.write(".github/workflows/tests.yml", "fixture workflow\n")
        self.base = self.commit("base")
        blob = self.git(self.publisher, "rev-parse", "HEAD:.github/workflows/tests.yml")
        self.workflows = (train_ci.Workflow(".github/workflows/tests.yml", ("suite",),
            sources=((".github/workflows/tests.yml", blob),)),)
        self.git(self.root, "clone", "-q", "--bare", self.publisher, self.origin)
        self.git(self.root, "clone", "-q", self.origin, self.host)
        self.git(self.publisher, "remote", "add", "origin", self.origin)
        self.git(self.publisher, "checkout", "-qb", "lot/member")
        self.write("content", "reviewed change\n")
        self.head = self.commit("member")
        self.git(self.publisher, "push", "-q", "origin", "lot/member")
        # The host's repository has never fetched the lot. The passage must
        # fetch it into its own clone before asking the historical provider.
        self.assertNotEqual(subprocess.run(["git", "-C", self.host, "cat-file", "-e", self.head],
                                          capture_output=True).returncode, 0)
        self.ops = storage.of(self.db).work
        item = work.add(self.db, title="runner fixture", app=self.project.name, source="fixture",
                        actor="fixture", branch="lot/member", branch_target="main")
        mark = self.ops.add_milestone(item["id"], "frozen", sha=self.head, actor="fixture",
                                      verdict=None, note="")
        from ameesh import merge_queue
        self.ops.queue_add(item["id"], target="main", frozen_sha=self.head, note="", actor="fixture",
                           source=merge_queue.source_of(self.host), frozen_id=mark["id"])
        self.receipts = {"commit:" + self.head: dataclasses.asdict(author(self.head))}
        for reader, family in (("r1", "claude"), ("r2", "deepseek")):
            mark = self.ops.add_milestone(item["id"], "verdict", sha=self.head,
                                          actor=reader, verdict="ok", note="")
            _, receipt = verdict(mark["id"], reader, family, commit=self.head)
            receipt = dataclasses.replace(receipt, milestone={key: mark[key]
                for key in ("id", "actor", "at_ts", "sha", "verdict")})
            self.receipts[receipt.subject] = dataclasses.asdict(receipt)
        self.provider = types.SimpleNamespace(ProvenanceError=RuntimeError,
            receipt_for_commit=lambda db, sha: self.receipts.get("commit:" + sha),
            receipt_for_milestone=lambda db, mid: self.receipts.get("milestone:%d" % mid))
        self.mechanics = types.SimpleNamespace(MechanicsError=train.TrainError,
            load=mock.Mock(side_effect=FixtureMechanics))
        self.notices, self.api_calls = [], []
        self.key = self.project.name + ":main"
        self.now = datetime.fromisoformat("2026-10-15T11:00:00+02:00").timestamp()
        self.db.execute("UPDATE merge_queue SET queued_at=to_timestamp(%s)", [self.now - 3600])
        api = mock.patch.object(train_ci.GitHub, "_api", side_effect=self.api)
        api.start()
        self.addCleanup(api.stop)

    def git(self, path, *args):
        return subprocess.check_output(["git", "-C", path, *args], text=True,
                                       stderr=subprocess.PIPE).strip()

    def write(self, name, content):
        path = os.path.join(self.publisher, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(content)

    def commit(self, message):
        self.git(self.publisher, "add", "-A")
        self.git(self.publisher, "commit", "-qm", message)
        return self.git(self.publisher, "rev-parse", "HEAD")

    def passage(self, **kw):
        return train_runner.Passage(**dict(dict(cfg=self.cfg, project=self.project, owner="runner-a",
            policy=train.Policy(enabled=True),
            policy_path=self.publisher, policy_commit=self.base, workflows=self.workflows,
            provider=self.provider, mechanics=self.mechanics,
            notify=lambda reason, state: self.notices.append(reason)), **kw))

    def api(self, path):
        self.api_calls.append(path)
        if path.startswith("contents/"):
            return [dict(type="file", sha=self.workflows[0].sources[0][1])]
        if "/jobs?" in path:
            return [dict(jobs=[dict(id=1, name="suite", status="completed", conclusion="success")])]
        plan = self.ops.train_read(self.key)["plan"]
        return [dict(workflow_runs=[dict(id=1, run_attempt=1, head_sha=plan["head"],
            head_branch=plan["branch"], path=self.workflows[0].path, event="push",
            status="completed", conclusion="success")])]

    def test_disabled_passage_has_no_storage_git_or_ci_effect(self):
        passage = self.passage(policy=train.Policy())
        with mock.patch.object(passage, "runtime", side_effect=AssertionError("must stay off")):
            self.assertEqual(passage.step(mock.Mock(), self.now), {"result": "off"})
        self.assertIsNone(self.ops.train_read(self.key))
        self.assertFalse(os.path.exists(self.cfg.state_dir))
        self.assertEqual(self.api_calls, [])

    def test_new_clone_reads_real_history_then_lands_exact_ci_head_across_connections(self):
        passage = self.passage()
        result = passage.step(self.db, self.now)
        self.assertEqual(result["result"], "ci", result)
        plan, epoch = result["plan"], passage.epoch
        self.assertEqual(plan["proof"]["members"][0]["policy"]["reviews"], 2)
        self.assertIn(self.head, plan["proof"]["members"][0]["history"]["reviews"]["coverage"])
        self.assertEqual(self.git(self.origin, "rev-parse", plan["branch"]), plan["head"])
        other_db = db_mod.connect(self.__class__.cfg)
        self.addCleanup(other_db.close)
        result = passage.step(other_db, self.now + 1)
        self.assertEqual(result["result"], "awaiting-fire", result)
        self.assertEqual(passage.epoch, epoch)
        runtime = passage.runtime(other_db)
        with mock.patch.object(work, "corrector", return_value="pilot"):
            runtime.fires.give(plan, actor="pilot")
        with mock.patch.object(train_fire.Fires, "_still_pilot", return_value=True):
            result = passage.step(other_db, self.now + 2)
        self.assertEqual(result["result"], "landed", result)
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), plan["head"])
        self.assertEqual(self.git(self.host, "rev-parse", "HEAD"), self.base)
        self.assertIsNone(passage.epoch)
        self.assertEqual(self.notices, [])

    def test_packet_enrollment_keeps_its_generation_through_cutoff_and_fresh_passage(self):
        original = self.ops.queue(None)[0]
        item_id = original["work_item_id"]
        # Known disposable history: remove this fixture's individual entry
        # without claiming a legacy-history qualification for any real lot.
        self.db.execute("DELETE FROM merge_queue WHERE work_item_id = %s", (item_id,))
        self.db.execute("UPDATE work_items SET state = 'qa' WHERE id = %s", (item_id,))
        marks = self.ops.milestones(item_id, None)
        freeze = next(mark for mark in marks if mark["id"] == original["frozen_id"])
        candidate = dict(work_item_id=item_id, branch=original["branch"], target="main",
                         causes=[], candidate=dict(sha=self.head, origin="frozen", declared=freeze))
        passage = self.passage()
        runtime = passage.runtime(self.db)
        preparation = train_candidates.Preparation(self.db, self.project,
            repository=runtime.repository, history=runtime.admissions.history,
            policy=runtime.admissions.policy)
        enrolled = train_candidates.Enrollment(preparation, runtime.admissions,
            initial=lambda db, **context: True).enroll(candidate, self.base, actor="fixture")
        entry = enrolled["entry"]
        self.assertGreater(entry["generation"], original["generation"])
        self.assertNotIn("targeted_ci", enrolled["proof"]["members"][0])
        self.assertEqual(self.ops.milestones(item_id, None), marks)
        self.assertEqual(self.api_calls, [])

        # Anchor decision times to the returned SQL fact, never wall-clock
        # progress. The allocation is one second after this fixture cutoff.
        slot = entry["queued_ts"] - 1
        rules = FixtureMechanics(self.publisher, self.base, self.project.name)
        rules.last_packet = lambda now: slot if now < slot + 3600 else slot + 3600
        self.mechanics.load.side_effect = None
        self.mechanics.load.return_value = rules
        self.assertEqual(passage.step(self.db, slot + 1), {"result": "idle"})
        self.assertIsNone(self.ops.train_read(self.key))
        self.assertEqual(self.api_calls, [])

        shutil.rmtree(runtime.repository.clone.path)
        other_db = db_mod.connect(self.__class__.cfg)
        self.addCleanup(other_db.close)
        restarted = self.passage()
        fresh = restarted.runtime(other_db)
        preparation = train_candidates.Preparation(other_db, self.project,
            repository=fresh.repository, history=fresh.admissions.history,
            policy=fresh.admissions.policy)
        # Unknown initial history after restart permits the typed replay,
        # with no new generation or journal entry.
        replay = train_candidates.Enrollment(preparation, fresh.admissions).enroll(
            candidate, self.base, actor="restarted-fixture")
        self.assertEqual(replay, enrolled)
        self.assertEqual(self.ops.milestones(item_id, None), marks)
        result = restarted.step(other_db, slot + 3600)
        self.assertEqual(result["result"], "ci", result)
        plan = result["plan"]
        self.assertEqual([train.identity(row) for row in plan["members"]], [train.identity(entry)])
        self.assertEqual(plan["proof"], enrolled["proof"])
        self.assertEqual(self.git(self.origin, "rev-parse", plan["branch"]), plan["head"])
        self.assertEqual(restarted.step(other_db, slot + 3601)["result"], "awaiting-fire")
        with mock.patch.object(work, "corrector", return_value="pilot"):
            restarted.runtime(other_db).fires.give(plan, actor="pilot")
        with mock.patch.object(train_fire.Fires, "_still_pilot", return_value=True):
            result = restarted.step(other_db, slot + 3602)
        self.assertEqual(result["result"], "landed", result)
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), plan["head"])
        self.assertEqual(self.ops.queue(None)[0]["generation"], entry["generation"])
        receipts = self.db.query(
            "SELECT generation, kind FROM integration_train_queue_receipts WHERE work_item_id = %s",
            (item_id,))
        self.assertEqual(receipts, [dict(generation=entry["generation"], kind="packet")])
        self.assertEqual(self.notices, [])

    def test_scheduled_passage_uses_compiled_calendar_with_real_sql_and_reservation(self):
        rules = FixtureMechanics(self.publisher, self.base, self.project.name)
        rules.next_packet = mock.Mock(return_value=self.now + 1234)
        self.mechanics.load.side_effect = None
        self.mechanics.load.return_value = rules
        passage = self.passage()
        result, deadline = passage.scheduled_step(self.db, self.now)
        self.assertEqual(result["result"], "ci", result)
        self.assertEqual(deadline, self.now + 1234)
        self.mechanics.load.assert_called_once()
        rules.next_packet.assert_called_once_with(self.now)
        self.assertEqual(self.passage(owner="competing").step(self.db, self.now)["result"], "busy")
        self.assertEqual(self.ops.train_read(self.key)["plan"]["head"], result["plan"]["head"])

    def test_release_calendar_cannot_replace_absent_packet_calendar(self):
        rules = FixtureMechanics(self.publisher, self.base, self.project.name)
        self.mechanics.load.side_effect = None
        self.mechanics.load.return_value = rules
        for fields in (dict(packet_cutoffs=None), dict(packet_cutoffs=()),
                       dict(last_packet=None), dict(next_packet=None)):
            with self.subTest(fields=fields), mock.patch.multiple(rules, **fields):
                with self.assertRaisesRegex(train.TrainError, "explicit compiled packet calendar"):
                    self.passage().step(self.db, self.now)
        self.assertEqual(self.db.query("SELECT target FROM merge_locks"), [])
        self.assertEqual(self.api_calls, [])

    def test_competing_runner_cannot_touch_the_reserved_train_or_share_its_clone(self):
        first, second = self.passage(), self.passage(owner="runner-b")
        first_runtime, second_runtime = first.runtime(self.db), second.runtime(self.db)
        self.assertNotEqual(first_runtime.repository.clone.path, second_runtime.repository.clone.path)
        self.assertNotEqual(first_runtime.repository.clone.path, radar.clone_dir(self.cfg, self.project.name))
        result = first.step(self.db, self.now)
        self.assertEqual(result["result"], "ci", result)
        self.assertEqual(second.step(self.db, self.now + 1), {"result": "busy"})
        self.assertFalse(os.path.exists(second_runtime.repository.clone.path))
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.base)

    def test_missing_provenance_cannot_publish_or_be_replaced_by_current_registry(self):
        self.receipts.clear()
        result = self.passage().step(self.db, self.now)
        self.assertEqual(result["result"], "empty", result)
        self.assertIn("unknown historical author", result["selection"][0]["reason"])
        self.assertEqual(self.git(self.origin, "for-each-ref", "--format=%(refname)",
                                  "refs/heads/integration/"), "")
        self.assertEqual(self.api_calls, [])

    def test_missing_member_history_defers_dependents_and_publishes_independent_member(self):
        original = self.ops.queue(None)[0]
        added = []
        for branch, base in (("lot/dependent", self.head), ("lot/independent", self.base)):
            self.git(self.publisher, "checkout", "-qb", branch, base)
            self.write(branch.rsplit("/", 1)[1], "independent reviewed fixture\n")
            head = self.commit(branch)
            self.git(self.publisher, "push", "-q", "origin", branch)
            item = work.add(self.db, title=branch, app=self.project.name, source="fixture",
                            actor="fixture", branch=branch, branch_target="main")
            freeze = self.ops.add_milestone(item["id"], "frozen", sha=head, actor="fixture",
                                           verdict=None, note="")
            member = self.ops.queue_add(item["id"], target="main", frozen_sha=head,
                note="", actor="fixture", source=original["source"], frozen_id=freeze["id"])
            self.receipts["commit:" + head] = dataclasses.asdict(author(head))
            for reader, family in (("r1", "claude"), ("r2", "deepseek")):
                mark = self.ops.add_milestone(item["id"], "verdict", sha=head,
                    actor=reader, verdict="ok", note="")
                _, receipt = verdict(mark["id"], reader, family, commit=head)
                receipt = dataclasses.replace(receipt, milestone={key: mark[key]
                    for key in ("id", "actor", "at_ts", "sha", "verdict")})
                self.receipts[receipt.subject] = dataclasses.asdict(receipt)
            added.append(member)
        dependent, independent = added
        storage.of(self.db).work_dependencies.add(dependent["work_item_id"],
            original["work_item_id"], actor="fixture")
        self.db.execute("UPDATE merge_queue SET queued_at=to_timestamp(%s)", [self.now - 3600])
        del self.receipts["commit:" + self.head]
        passage = self.passage()
        with mock.patch.object(train_ci.GitHub, "start") as dispatch:
            result = passage.step(self.db, self.now)
        self.assertEqual(result["result"], "ci", result)
        self.assertEqual([row["work_item_id"] for row in result["plan"]["members"]],
                         [independent["work_item_id"]])
        state = self.ops.train_read(self.key)
        self.assertEqual({row["work_item_id"] for row in state["deferred"]},
                         {original["work_item_id"], dependent["work_item_id"]})
        self.assertIn("unknown historical author", state["selection"][0]["reason"])
        self.assertEqual(state["selection"][0]["admission"], list(train.identity(original)))
        dispatch.assert_called_once()
        self.assertTrue(all(row["state"] == "queued" for row in self.ops.queue(None)))
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.base)

    def test_provider_and_ci_contract_are_required_before_reserving_target(self):
        for overrides, message in ((dict(provider=None), "provenance provider"),
                                   (dict(mechanics=None), "mechanics provider"),
                                   (dict(policy=train.Policy(enabled=True, build_beta=False)), "canon cutoffs"),
                                   (dict(workflows=()), "complete required workflow"),
                                   (dict(policy_commit="HEAD"), "exact audited canon commit")):
            with self.subTest(overrides=overrides), self.assertRaisesRegex(
                    (train.TrainError, ValueError), message):
                self.passage(**overrides).step(self.db, self.now)
        self.assertEqual(self.db.query("SELECT target FROM merge_locks"), [])

    def test_compiled_calendar_controls_admission_cutoff_and_survives_restart(self):
        rules = FixtureMechanics(self.publisher, self.base, self.project.name)
        # The calendar is a trusted compiled port. Choosing times relative
        # to the actual queue timestamp keeps this independent of wall time.
        queued = self.ops.queue(None)[0]["queued_ts"]
        rules.last_packet = lambda now: queued - 1 if now < queued + 100 else queued + 100
        self.mechanics.load.side_effect = None
        self.mechanics.load.return_value = rules
        passage = self.passage(policy=train.Policy(enabled=True))
        self.assertEqual(passage.step(self.db, queued + 50)["result"], "idle")
        self.assertEqual(self.api_calls, [])
        result = passage.step(self.db, queued + 100)
        self.assertEqual(result["result"], "ci", result)
        self.assertEqual(result["plan"]["proof"]["members"][0]["policy"]["policy_commit"], self.base)
        self.assertEqual(passage.step(self.db, queued + 101)["result"], "awaiting-fire")
        self.assertEqual(len(self.ops.train_read(self.key)["attempts"]), 1)
        self.mechanics.load.assert_called_with(self.publisher, self.base, self.project.name)

    def dst_passage(self, epochs):
        # Explicit consumer fixture, not an implementation of the calendar.
        # The cross-branch probe overrides this with #1757's real loader.
        rules = FixtureMechanics(self.publisher, self.base, self.project.name)
        rules.merge_named_fire = False
        rules.packet_cutoffs = tuple("%02d:00" % hour for hour in range(24))
        rules.last_packet = lambda now: max(at for at in epochs if at <= now)
        rules.next_packet = lambda now: min(at for at in epochs if at > now)
        self.mechanics.load.side_effect = None
        self.mechanics.load.return_value = rules
        return self.passage()

    def pending_packet_across_dst(self, instants):
        first, second, following = [datetime.fromisoformat(at).timestamp() for at in instants]
        self.db.execute("UPDATE merge_queue SET queued_at=to_timestamp(%s)", [first - 1])
        passage = self.dst_passage((first, second, following))
        rules = passage.runtime(self.db).policy.schedule
        self.assertEqual(rules.last_packet(first + 5), first)
        self.assertEqual(rules.last_packet(second + 5), second)
        start = train_ci.GitHub.start
        with mock.patch.object(train_ci.GitHub, "start", autospec=True, side_effect=start) as ci_start:
            result, deadline = passage.scheduled_step(self.db, first + 5)
            self.assertEqual(result["result"], "ci", result)
            self.assertEqual(deadline, second)
            plan = result["plan"]
            other_db = db_mod.connect(self.__class__.cfg)
            self.addCleanup(other_db.close)
            resumed = self.dst_passage((first, second, following))
            pending = dict(head=plan["head"], tree=plan["tree"], status="pending", complete=False)
            with mock.patch.object(train_ci.GitHub, "read", return_value=pending):
                result, deadline = resumed.scheduled_step(other_db, second + 5)
            self.assertEqual(result["result"], "ci", result)
            self.assertEqual(train.digest(result["plan"]), train.digest(plan))
            self.assertEqual(deadline, following)
            self.assertEqual(len(self.ops.train_read(self.key)["attempts"]), 1)
            self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.base)
            result, _ = resumed.scheduled_step(other_db, second + 10)
            self.assertEqual(result["result"], "landed", result)
            self.assertEqual(self.git(self.origin, "rev-parse", "main"), plan["head"])
            ci_start.assert_called_once()
        self.assertEqual(self.git(self.origin, "for-each-ref", "--format=%(refname)",
                                  "refs/heads/integration/").splitlines(),
                         ["refs/heads/" + plan["branch"]])

    def test_pending_packet_survives_repeated_hour_on_fresh_connection(self):
        self.pending_packet_across_dst(("2026-10-25T02:00:00+02:00",
                                       "2026-10-25T02:00:00+01:00",
                                       "2026-10-25T03:00:00+01:00"))

    def test_pending_packet_survives_spring_gap_on_fresh_connection(self):
        self.pending_packet_across_dst(("2026-03-29T01:00:00+01:00",
                                       "2026-03-29T03:00:00+02:00",
                                       "2026-03-29T04:00:00+02:00"))

    def test_missing_invalid_or_incompatible_compiled_policy_refuses_before_effect(self):
        rules = FixtureMechanics(self.publisher, self.base, self.project.name)
        self.mechanics.load.side_effect = train.TrainError("mechanics absent")
        with self.assertRaisesRegex(train.TrainError, "mechanics absent"):
            self.passage().step(self.db, self.now)
        self.mechanics.load.side_effect = None
        self.mechanics.load.return_value = rules
        for fields in (dict(ci_packet="impacted"), dict(ci_packet="none"),
                       dict(commit="a" * 40), dict(project="other")):
            modified = types.SimpleNamespace(**dict(vars(rules), **fields))
            self.mechanics.load.return_value = modified
            with self.subTest(fields=fields), self.assertRaises(train.TrainError):
                self.passage().step(self.db, self.now)
        self.assertEqual(self.db.query("SELECT target FROM merge_locks"), [])
        self.assertEqual(self.api_calls, [])
        self.assertFalse(os.path.exists(self.cfg.state_dir))

    def test_compiled_families_are_used_by_real_historical_admission(self):
        rules = FixtureMechanics(self.publisher, self.base, self.project.name)
        rules.families = ("codex", "claude")
        self.mechanics.load.side_effect = None
        self.mechanics.load.return_value = rules
        result = self.passage().step(self.db, self.now)
        self.assertEqual(result["result"], "empty", result)
        self.assertIn("missing independent", result["selection"][0]["reason"])
        self.assertEqual(self.api_calls, [])

    def test_lost_member_history_during_ci_preserves_plan_until_terminal_outcome(self):
        passage = self.passage()
        with mock.patch.object(train_ci.GitHub, "start") as dispatch:
            plan = passage.step(self.db, self.now)["plan"]
            del self.receipts["commit:" + self.head]
            result = passage.step(self.db, self.now + 1)
        self.assertEqual(result["result"], "refused", result)
        self.assertIn("unknown historical author", result["error"])
        state = self.ops.train_read(self.key)
        self.assertEqual(state["phase"], "draining")
        self.assertEqual(state["plan"], json.loads(json.dumps(plan)))
        self.assertEqual(len(state["attempts"]), 1)
        self.assertEqual(state["deferred"], [])
        dispatch.assert_called_once()
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.base)
        self.assertIsNotNone(passage.epoch)

    def automatic_passage(self):
        rules = FixtureMechanics(self.publisher, self.base, self.project.name)
        rules.merge_named_fire = False
        self.mechanics.load.side_effect = None
        self.mechanics.load.return_value = rules
        return self.passage()

    def test_compiled_automatic_policy_lands_green_packet_without_pilot_across_connections(self):
        passage = self.automatic_passage()
        plan = passage.step(self.db, self.now)["plan"]
        other_db = db_mod.connect(self.__class__.cfg)
        self.addCleanup(other_db.close)
        with mock.patch.object(work, "corrector", side_effect=AssertionError("no named fire")):
            result = passage.step(other_db, self.now + 1)
        self.assertEqual(result["result"], "landed", result)
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), plan["head"])
        self.assertIsNone(self.ops.train_fire_read(self.key, plan["id"]))
        state = self.ops.train_read(self.key)
        self.assertEqual(state["fire"]["policy_commit"], self.base)
        self.assertFalse(state["fire"]["merge"]["named_fire"])
        self.assertIsNone(passage.epoch)

    def test_qualified_transport_survives_sql_restart_and_missing_clone_without_refreeze(self):
        self.automatic_passage()  # install the explicit fixture's automatic policy
        member = self.ops.queue(None)[0]
        source_identity = list(train.identity(member))
        self.git(self.publisher, "checkout", "main")
        self.git(self.publisher, "merge", "--no-ff", "--no-edit", self.head)
        self.write("adapted-test", "qualified adaptation fixture\n")
        self.git(self.publisher, "add", "adapted-test")
        self.git(self.publisher, "commit", "--amend", "--no-edit")
        head = self.git(self.publisher, "rev-parse", "HEAD")
        receipt = dict(schema=train_transports.SCHEMA, source=member["source"], target="main",
            admission=source_identity, base=self.base, head=head,
            tree=self.git(self.publisher, "rev-parse", "HEAD^{tree}"),
            delta=train_transports.delta(self.publisher, self.base, head),
            ref="refs/ameesh/resolutions/%d/qualified-fixture" % member["work_item_id"],
            qualification=dict(kind="adapted", references=[dict(kind="work-event", id=1)]))
        self.git(self.publisher, "push", "-q", "origin", head + ":" + receipt["ref"])
        connections = []
        def reader(db, **context):
            connections.append(db)
            return json.loads(json.dumps(receipt))
        passage = self.passage(transports=reader)
        plan = passage.step(self.db, self.now)["plan"]
        self.assertEqual(plan["transports"], [receipt])
        self.assertEqual(list(train.identity(plan["members"][0])), source_identity)
        self.assertNotIn("transports", plan["proof"])
        shutil.rmtree(passage.repository_path())
        other = db_mod.connect(self.__class__.cfg)
        self.addCleanup(other.close)
        result = passage.step(other, self.now + 1)
        self.assertEqual(result["result"], "landed", result)
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), plan["head"])
        self.assertIn(head, self.git(self.origin, "rev-list", "--parents", "-n", "1", plan["head"]).split()[1:])
        self.assertIs(connections[-1], other)
        state = self.ops.train_read(self.key)
        self.assertEqual(len(state["attempts"]), 1)
        self.assertEqual(state["plan"], json.loads(json.dumps(plan)))
        self.assertEqual(list(train.identity(self.ops.queue(None)[0])), source_identity)
        rows = passage.incident_facts(other).lands("o/r", plan["head"])
        self.assertEqual(rows[0]["files"], ["adapted-test", "content"])
        self.assertEqual(rows[0]["sha"], self.head)

    def indexed_transport(self, *, kind="adapted"):
        self.automatic_passage()
        member = self.ops.queue(None)[0]
        self.git(self.publisher, "checkout", "main")
        self.git(self.publisher, "merge", "--no-ff", "--no-edit", self.head)
        self.write("adapted-test", "existing qualified adaptation fixture\n")
        self.git(self.publisher, "add", "adapted-test")
        self.git(self.publisher, "commit", "--amend", "--no-edit")
        head = self.git(self.publisher, "rev-parse", "HEAD")
        ref = "refs/ameesh/resolutions/%d/journal-fixture" % member["work_item_id"]
        self.git(self.publisher, "push", "-q", "origin", head + ":" + ref)
        work.note(self.db, member["work_item_id"], "existing qualification fixture " + head,
                  actor="fixture-pilot")
        original = self.ops.events(member["work_item_id"], 1)[0]
        qualification = dict(kind=kind, references=[dict(kind="work-event", id=original["id"])])
        self.import_transport = lambda db=self.db: train_transport_journal.import_ref(
            db, self.project, self.publisher, ref, lot=member["work_item_id"],
            source_head=self.head, qualification=qualification, actor="fixture-importer")
        document = self.import_transport()
        self.transport_connections = []

        def resolve(db, *, facts, qualification, originals, history):
            # Authority remains an explicit fixture. All supplied sources and
            # their original authors are nevertheless read from real SQL.
            self.transport_connections.append(db)
            self.assertEqual(originals[0]["author"], "fixture-pilot")
            self.assertEqual(originals[0]["record"], original)
            self.assertEqual(facts, document["facts"])
            active = not any(r["note"] == "withdraw fixture qualification" for r in history["events"])
            return dict(facts=facts, qualification=qualification, active=active)
        reader = train_transport_journal.Recorded(self.project, resolve=resolve)
        return member, document, reader

    def test_journal_import_real_sources_to_plan_then_fresh_connection_and_lost_clone(self):
        member, document, reader = self.indexed_transport()
        self.assertNotIn("admission", document)
        self.assertNotIn("generation", document["facts"])
        self.assertEqual(self.import_transport(), document)
        indexed = self.ops.events_noted(train_transport_journal._prefix(member["work_item_id"]), 0)
        self.assertEqual(len(indexed), 1)
        passage = self.passage(transports=reader)
        plan = passage.step(self.db, self.now)["plan"]
        self.assertEqual(plan["transports"][0]["head"], document["facts"]["head"])
        self.assertEqual(plan["transports"][0]["admission"], list(train.identity(member)))
        shutil.rmtree(passage.repository_path())
        other = db_mod.connect(self.__class__.cfg)
        self.addCleanup(other.close)
        resumed = self.passage(transports=train_transport_journal.Recorded(self.project, resolve=reader.resolve))
        result = resumed.step(other, self.now + 1)
        self.assertEqual(result["result"], "landed", result)
        self.assertIs(self.transport_connections[-1], other)
        state = self.ops.train_read(self.key)
        self.assertEqual(state["plan"], json.loads(json.dumps(plan)))
        self.assertEqual(len(state["attempts"]), 1)
        self.assertEqual(list(train.identity(self.ops.queue(None)[0])), list(train.identity(member)))

    def test_normalized_original_author_qualification_to_plan_then_lost_clone_on_fresh_connection(self):
        member, document, _ = self.indexed_transport(kind="mechanical")
        facts = document["facts"]
        normalization = dict(schema=train_transport_journal.QUALIFICATION_SCHEMA,
            work_item_id=member["work_item_id"], frozen_sha=facts["source_head"],
            base=facts["base"], head=facts["head"], kind="mechanical", state="maintained",
            source_reference=document["qualification"]["references"][0])
        work.note(self.db, member["work_item_id"], json.dumps(normalization), actor="fixture-pilot")
        reader = train_transport_journal.Recorded(self.project, resolve=train_transport_journal.Normalized())
        passage = self.passage(transports=reader)
        result = passage.step(self.db, self.now)
        self.assertEqual(result["result"], "ci", result)
        plan = result["plan"]
        self.assertEqual(plan["transports"][0]["qualification"], document["qualification"])
        shutil.rmtree(passage.repository_path())
        other = db_mod.connect(self.__class__.cfg)
        self.addCleanup(other.close)
        result = self.passage(transports=reader).step(other, self.now + 1)
        self.assertEqual(result["result"], "landed", result)
        state = self.ops.train_read(self.key)
        self.assertEqual(state["plan"], json.loads(json.dumps(plan)))
        self.assertEqual(len(state["attempts"]), 1)
        self.assertEqual(list(train.identity(self.ops.queue(None)[0])), list(train.identity(member)))

    def test_journal_reader_rechecks_withdrawal_before_promotion_on_fresh_connection(self):
        member, _, reader = self.indexed_transport()
        passage = self.passage(transports=reader)
        passage.step(self.db, self.now)
        other = db_mod.connect(self.__class__.cfg)
        self.addCleanup(other.close)
        work.note(other, member["work_item_id"], "withdraw fixture qualification", actor="fixture-pilot")
        result = passage.step(other, self.now + 1)
        self.assertEqual(result["result"], "refused", result)
        self.assertIn("withdrawn", result["error"])
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.base)
        self.assertEqual(len(self.ops.train_read(self.key)["attempts"]), 1)

    def test_journal_reader_refuses_missing_originals_resolver_stale_context_and_ref(self):
        member, document, reader = self.indexed_transport()
        path = self.publisher
        context = dict(source=member["source"], target="main", row=member, base=self.base, path=path)
        with self.assertRaisesRegex(train.TrainError, "resolver unavailable"):
            train_transport_journal.Recorded(self.project)(self.db, **context)
        with self.assertRaisesRegex(train.TrainError, "current freeze and intermediate base"):
            reader(self.db, **dict(context, base=self.head))
        reader.resolve = lambda db, **kw: dict(facts=dict(kw["facts"], target="another"),
                                              qualification=kw["qualification"], active=True)
        with self.assertRaisesRegex(train.TrainError, "outside context"):
            reader(self.db, **context)
        original = document["qualification"]["references"][0]["id"]
        with self.db.transaction() as tx:
            tx.execute("DELETE FROM work_item_events WHERE id = %s", (original,))
            with self.assertRaisesRegex(train.TrainError, "absent or outside lot"):
                reader(tx, **context)
        self.git(self.publisher, "push", "-q", "origin", self.head + ":" + document["facts"]["ref"], "--force")
        with self.assertRaisesRegex(train.TrainError, "withdrawn or moved"):
            reader(self.db, **context)

    def test_journal_sources_keep_exact_milestone_mail_and_manifest_without_delivery_metadata(self):
        lot = self.ops.queue(None)[0]["work_item_id"]
        mark = self.ops.add_milestone(lot, "verdict", sha=self.head,
                                      actor="fixture-pilot", verdict="ok", note="existing fixture")
        mailbox = storage.of(self.db).mailbox
        message = mailbox.send("fixture-pilot", "fixture-reader", "private qualification fixture",
            host=None, kind="notify", payload={}, work_item_id=str(lot), signature=None,
            signature_key=None, signed_payload=None, nonce=None, created_us=None, expires_us=None)
        references = [dict(kind="milestone", id=mark["id"]), dict(kind="mail", id=message["id"]),
                      dict(kind="manifest", commit=self.base, path=".github/workflows/tests.yml")]
        before = train_transport_journal.originals(self.db, self.publisher, lot, references)
        self.assertEqual([r["reference"] for r in before], references)
        mailbox.mark_delivered([message["id"]])
        self.assertEqual(train_transport_journal.originals(self.db, self.publisher, lot, references), before)
        indexed = train_transport_journal._fingerprints(before)
        self.assertNotIn("private qualification fixture", json.dumps(indexed))
        self.assertEqual(indexed[1]["author"], "fixture-pilot")

    def test_journal_snapshot_uses_current_generation_and_refuses_ambiguous_or_changed_originals(self):
        member, document, reader = self.indexed_transport()
        context = dict(source=member["source"], target="main", row=member, base=self.base, path=self.publisher)
        next_member = dict(member, generation=member["generation"] + 1, frozen_id=member["frozen_id"] + 1)
        receipt = reader(self.db, **dict(context, row=next_member))
        self.assertEqual(receipt["admission"], list(train.identity(next_member)))
        self.assertNotIn("admission", self.import_transport())
        original = document["qualification"]["references"][0]["id"]
        self.db.execute("UPDATE work_item_events SET actor = 'changed' WHERE id = %s", (original,))
        with self.assertRaisesRegex(train.TrainError, "original transport qualification facts changed"):
            reader(self.db, **context)
        self.db.execute("UPDATE work_item_events SET actor = 'fixture-pilot' WHERE id = %s", (original,))
        alternative = json.loads(json.dumps(document))
        alternative["qualification"]["kind"] = "mechanical"
        work.note(self.db, member["work_item_id"], train_transport_journal._prefix(member["work_item_id"])
                  + json.dumps(alternative), actor="another-importer")
        with self.assertRaisesRegex(train.TrainError, "ambiguous existing transport qualification"):
            reader(self.db, **context)

    def test_automatic_claim_before_effect_crash_is_never_replayed(self):
        passage = self.automatic_passage()
        plan = passage.step(self.db, self.now)["plan"]
        with mock.patch.object(train_fire.Automatic, "promote", side_effect=train.TrainError("crash")):
            self.assertEqual(passage.step(self.db, self.now + 1)["result"], "refused")
        self.assertEqual(self.ops.train_read(self.key)["phase"], "promoting")
        restarted = self.automatic_passage()
        with mock.patch.object(train_git.Repository, "promote", side_effect=AssertionError("no replay")):
            result = restarted.step(self.db, self.now + 2)
        self.assertEqual(result["result"], "refused", result)
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.base)
        self.assertEqual(self.ops.train_read(self.key)["plan"]["id"], plan["id"])

    def test_automatic_lost_push_response_reconciles_without_second_push(self):
        passage = self.automatic_passage()
        plan = passage.step(self.db, self.now)["plan"]
        promote = train_git.Repository.promote
        def lost_response(repository, *args, **kwargs):
            promote(repository, *args, **kwargs)
            raise train.TrainError("response lost")
        with mock.patch.object(train_git.Repository, "promote", new=lost_response):
            self.assertEqual(passage.step(self.db, self.now + 1)["result"], "refused")
        self.assertEqual(self.ops.train_read(self.key)["phase"], "promoting")
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), plan["head"])
        with mock.patch.object(train_git.Repository, "promote", side_effect=AssertionError("no replay")):
            result = self.automatic_passage().step(self.db, self.now + 2)
        self.assertEqual(result["result"], "landed", result)
        self.assertEqual(self.ops.train_read(self.key)["phase"], "landed")

    def assert_automatic_recheck(self, invalidate):
        passage = self.automatic_passage()
        passage.step(self.db, self.now)
        authorize, api = train_fire.Automatic.authorize, self.api
        def changed(authority, plan):
            receipt = authorize(authority, plan)
            if invalidate == "review":
                self.receipts.clear()
            else:
                train_ci.GitHub._api.side_effect = lambda path: (
                    [dict(workflow_runs=[])] if "/runs?" in path else api(path))
            return receipt
        with mock.patch.object(train_fire.Automatic, "authorize", new=changed), \
                mock.patch.object(train_git.Repository, "promote") as effect:
            result = passage.step(self.db, self.now + 1)
        self.assertEqual(result["result"], "refused", result)
        self.assertEqual(self.ops.train_read(self.key)["phase"], "promoting")
        effect.assert_not_called()

    def test_automatic_rechecks_reviews_after_persisted_claim(self):
        self.assert_automatic_recheck("review")

    def test_automatic_rechecks_ci_after_persisted_claim(self):
        self.assert_automatic_recheck("ci")

    def test_missing_merge_contract_never_falls_back_to_named_fire(self):
        rules = FixtureMechanics(self.publisher, self.base, self.project.name)
        fields = dict(vars(rules), schema=rules.schema, ci_packet=rules.ci_packet)
        self.mechanics.load.side_effect = None
        self.mechanics.load.return_value = types.SimpleNamespace(**fields)
        with self.assertRaisesRegex(train.TrainError, "explicit compiled packet merge policy"):
            self.passage().step(self.db, self.now)
        self.assertEqual(self.db.query("SELECT target FROM merge_locks"), [])
        self.assertEqual(self.api_calls, [])

    def test_red_packet_reports_unknown_causes_without_accusing_a_member_or_starting_more_ci(self):
        passage = self.automatic_passage()
        plan = passage.step(self.db, self.now)["plan"]
        api = self.api
        def failed(path):
            rows = api(path)
            if "/jobs?" in path:
                rows[0]["jobs"][0]["conclusion"] = "failure"
            elif "/runs?" in path:
                rows[0]["workflow_runs"][0]["conclusion"] = "failure"
            return rows
        train_ci.GitHub._api.side_effect = failed
        with mock.patch.object(train_ci.GitHub, "start") as start, \
                mock.patch.object(train_git.Repository, "promote") as promote:
            result = passage.step(self.db, self.now + 1)
        self.assertEqual(result["result"], "paused", result)
        report = self.ops.train_read(self.key)["diagnosis"]
        self.assertEqual(report["packet"]["head"], plan["head"])
        self.assertEqual(report["ci"][0]["attempt"], 1)
        self.assertEqual(report["environment"]["status"], "unknown")
        self.assertEqual(report["attribution"]["proven_lots"], [])
        self.assertTrue(report["missing"])
        start.assert_not_called()
        promote.assert_not_called()
        self.assertIsNone(passage.epoch)

    def test_post_landing_facts_read_sql_manifest_on_fresh_connection_without_ci(self):
        passage = self.automatic_passage()
        plan = passage.step(self.db, self.now)["plan"]
        self.assertEqual(passage.step(self.db, self.now + 1)["result"], "landed")
        runtime = passage.runtime(self.db)
        other = db_mod.connect(self.__class__.cfg)
        self.addCleanup(other.close)
        with mock.patch.object(passage, "runtime", side_effect=AssertionError("current runtime")):
            facts = passage.incident_facts(other)
        self.assertEqual(facts.path, runtime.repository.clone.path)
        calls = list(self.api_calls)
        with mock.patch.object(train_ci.GitHub, "read", side_effect=AssertionError("CI read")), \
                mock.patch.object(train_git.Repository, "target", side_effect=AssertionError("fetch")):
            rows = facts.lands("o/r", plan["head"])
            self.assertIsNone(facts.diagnoses("o/r", plan["head"]))
            self.assertIsNone(facts.links_for("o/r", plan["head"]))
        self.assertEqual([(r["sha"], r["lot"], r["files"], r["author"]) for r in rows],
                         [(self.head, plan["members"][0]["work_item_id"], ["content"], "")])
        self.assertEqual(self.api_calls, calls)
        self.assertIsNone(passage.epoch)
