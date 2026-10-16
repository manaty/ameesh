# SPDX-License-Identifier: AGPL-3.0-only
import copy
import json
import types
import unittest

from ameesh import train, train_ci, train_contracts


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
        self.blob = "d" * 40

    def gh(self, args):
        self.calls.append(args)
        if args[0] == "workflow":
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        if "/contents/" in args[-1]:
            payload = dict(type="file", sha=self.blob)
        else:
            payload = dict(jobs=self.jobs) if "/jobs?" in args[-1] else dict(workflow_runs=self.runs)
        return types.SimpleNamespace(returncode=0, stdout=json.dumps([payload]), stderr="")

    def ci(self, event="push"):
        return train_ci.GitHub("o/r", (train_ci.Workflow(".github/workflows/tests.yml",
                                                         ("suite",), event,
                                      sources=((".github/workflows/tests.yml", "d" * 40),)),),
                              gh=self.gh)

    def test_push_uses_existing_exact_run_without_second_dispatch(self):
        ci = self.ci()
        ci.start(self.plan)
        self.assertFalse(any(c[0] == "workflow" for c in self.calls))
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
        self.assertEqual(self.calls[-1], ["workflow", "run", ".github/workflows/tests.yml",
                                      "--repo", "o/r", "--ref", self.plan["branch"],
                                      "-f", "expected_sha=" + self.plan["head"]])

    def test_changed_full_suite_source_refuses_before_dispatch_or_evidence(self):
        self.blob = "e" * 40
        for action in (self.ci("workflow_dispatch").start, self.ci().read):
            with self.assertRaisesRegex(train.TrainError, "source differs"):
                action(self.plan)
        self.assertFalse(any(c[0] == "workflow" for c in self.calls))

    def test_no_unpinned_or_ambiguous_workflow_contract(self):
        with self.assertRaisesRegex(ValueError, "audited source"):
            train_ci.Workflow(".github/workflows/tests.yml", ("suite",))
        with self.assertRaisesRegex(ValueError, "audited source"):
            train_ci.Workflow(".github/workflows/tests.yml", ("suite",),
                              sources=((".github/workflows/tests.yml", "d" * 40),) * 2)

    def test_complete_red_needs_every_required_job_executed(self):
        self.runs = [dict(self.run, conclusion="failure")]
        self.jobs = [dict(self.job, conclusion="failure")]
        result = self.ci().read(self.plan)
        self.assertEqual((result["status"], result["complete"]), ("red", True))
        for conclusion in ("skipped", "cancelled", "timed_out"):
            self.jobs = [dict(self.job, conclusion=conclusion)]
            with self.assertRaisesRegex(train.TrainError, "skipped or interrupted"):
                self.ci().read(self.plan)

    def test_ancillary_failure_and_allowed_test_failure_cannot_blame_or_vouch(self):
        for run_conclusion, job_conclusion in (("failure", "success"), ("success", "failure")):
            self.runs = [dict(self.run, conclusion=run_conclusion)]
            self.jobs = [dict(self.job, conclusion=job_conclusion)]
            with self.assertRaisesRegex(train.TrainError, "does not prove"):
                self.ci().read(self.plan)

    def test_ameesh_aggregate_cannot_cover_missing_matrix_part_or_platform(self):
        contract, = train_contracts.ameesh()
        sources = dict(contract.sources)
        def gh(args):
            path = args[-1]
            if "/contents/" in path:
                source = path.split("/contents/", 1)[1].split("?", 1)[0]
                payload = dict(type="file", sha=sources[source])
            elif "/jobs?" in path:
                payload = dict(jobs=self.jobs)
            else:
                payload = dict(workflow_runs=self.runs)
            return types.SimpleNamespace(returncode=0, stdout=json.dumps([payload]))
        ci = train_ci.GitHub("o/r", (contract,), gh=gh)
        every = [dict(self.job, id=i, name=name) for i, name in enumerate(contract.jobs)]
        self.jobs = every
        self.assertEqual(ci.read(self.plan)["status"], "green")
        for missing in ("psycopg 4/4", "psql 6/6", "plateforme macOS"):
            with self.subTest(missing=missing):
                self.jobs = [j for j in every if j["name"] != missing]
                with self.assertRaisesRegex(train.TrainError, "required full-suite job missing"):
                    ci.read(self.plan)
