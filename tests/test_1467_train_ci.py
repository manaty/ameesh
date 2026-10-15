# SPDX-License-Identifier: AGPL-3.0-only
import copy
import json
import types
import unittest

from ameesh import train, train_ci


class TrainCiTest(unittest.TestCase):
    def setUp(self):
        self.plan = dict(head="a" * 40, tree="b" * 40, branch="integration/train-test")
        self.run = dict(id=9, run_attempt=1, status="completed", conclusion="success",
                        head_sha=self.plan["head"], head_branch=self.plan["branch"],
                        path=".github/workflows/tests.yml", event="push")
        self.job = dict(id=19, name="suite", status="completed", conclusion="success")
        self.calls = []
        self.runs = [self.run]
        self.jobs = [self.job]

    def gh(self, args):
        self.calls.append(args)
        if args[0] == "workflow":
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        payload = dict(jobs=self.jobs) if "/jobs?" in args[-1] else dict(workflow_runs=self.runs)
        return types.SimpleNamespace(returncode=0, stdout=json.dumps([payload]), stderr="")

    def ci(self, event="push"):
        return train_ci.GitHub("o/r", (train_ci.Workflow(".github/workflows/tests.yml",
                                                         ("suite",), event),), gh=self.gh)

    def test_push_uses_existing_exact_run_without_second_dispatch(self):
        ci = self.ci()
        ci.start(self.plan)
        self.assertEqual(self.calls, [])
        result = ci.read(self.plan)
        self.assertEqual((result["status"], result["complete"]), ("green", True))
        self.assertEqual(result["workflows"][0]["run"], 9)

    def test_other_branch_other_head_other_workflow_cannot_vouch(self):
        for key, value in (("head_branch", "other"), ("head_sha", "c" * 40),
                           ("path", ".github/workflows/other.yml"), ("event", "workflow_dispatch")):
            with self.subTest(key=key):
                self.runs = [dict(self.run, **{key: value})]
                self.assertEqual(self.ci().read(self.plan)["status"], "absent")

    def test_skipped_required_job_is_not_full_ci(self):
        self.jobs = [dict(self.job, conclusion="skipped")]
        with self.assertRaisesRegex(train.TrainError, "skipped"):
            self.ci().read(self.plan)

    def test_latest_attempt_cannot_inherit_earlier_green(self):
        self.runs = [self.run, dict(self.run, run_attempt=2, conclusion=None, status="in_progress")]
        self.assertEqual(self.ci().read(self.plan)["status"], "pending")

    def test_cancelled_ci_does_not_blame_a_lot(self):
        self.runs = [dict(self.run, conclusion="cancelled")]
        with self.assertRaisesRegex(train.TrainError, "no culpable lot"):
            self.ci().read(self.plan)

    def test_nexlink_dispatch_pins_the_exact_sha(self):
        self.ci("workflow_dispatch").start(self.plan)
        self.assertEqual(self.calls, [["workflow", "run", ".github/workflows/tests.yml",
                                      "--repo", "o/r", "--ref", self.plan["branch"],
                                      "-f", "expected_sha=" + self.plan["head"]]])
