# SPDX-License-Identifier: AGPL-3.0-only
"""One passage across real queue, policy Git, clone, leases and promotion.

CI and the provenance provider are explicit fixtures, never live services.
"""
import dataclasses
import json
import os
import subprocess
import tempfile
import time
import types
from unittest import mock

from ameesh import (db as db_mod, radar, storage, train, train_ci, train_fire,
                    train_runner, work)

from .support import PgTestCase
from .test_1467_train_review import author, verdict


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
        self.notices, self.api_calls = [], []
        self.key, self.now = self.project.name + ":main", time.time()
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
            policy=train.Policy(enabled=True, build_beta=False, threshold=1),
            policy_path=self.publisher, policy_commit=self.base, workflows=self.workflows,
            provider=self.provider, notify=lambda reason, state: self.notices.append(reason)), **kw))

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
        self.assertEqual(result["result"], "refused", result)
        self.assertIn("unknown historical author", result["error"])
        self.assertEqual(self.git(self.origin, "for-each-ref", "--format=%(refname)",
                                  "refs/heads/integration/"), "")
        self.assertEqual(self.api_calls, [])

    def test_provider_and_ci_contract_are_required_before_reserving_target(self):
        for overrides, message in ((dict(provider=None), "provenance provider"),
                                   (dict(workflows=()), "complete required workflow"),
                                   (dict(policy_commit="HEAD"), "exact audited canon commit")):
            with self.subTest(overrides=overrides), self.assertRaisesRegex(
                    (train.TrainError, ValueError), message):
                self.passage(**overrides).step(self.db, self.now)
        self.assertEqual(self.db.query("SELECT target FROM merge_locks"), [])
