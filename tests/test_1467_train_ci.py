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

    def test_gh_without_slurp_reads_concatenated_pages(self):
        """#2295 : le gh 2.45 de l'hôte refuse --slurp ; le repli sans --slurp
        décode les pages concaténées et rend la même preuve exacte."""
        def gh(args):
            self.calls.append(args)
            if "--slurp" in args:
                return types.SimpleNamespace(returncode=1, stdout="",
                                             stderr="unknown flag: --slurp")
            if "/contents/" in args[-1]:
                payload = dict(type="file", sha=self.blob)
            elif "/jobs?" in args[-1]:
                payload = dict(jobs=self.jobs)
            else:
                # deux pages concaténées, comme `--paginate` sans `--slurp`
                return types.SimpleNamespace(
                    returncode=0, stderr="",
                    stdout=json.dumps(dict(workflow_runs=[self.run]))
                    + json.dumps(dict(workflow_runs=[dict(self.run, id=10,
                                                          head_sha="c" * 40)])))
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
        ci = train_ci.GitHub("o/r", (train_ci.Workflow(
            ".github/workflows/tests.yml", ("suite",), "push",
            sources=((".github/workflows/tests.yml", "d" * 40),)),), gh=gh)
        result = ci.read(self.plan)
        self.assertEqual((result["status"], result["complete"]), ("green", True))
        self.assertEqual(result["workflows"][0]["run"], 9)
        self.assertTrue(any("--slurp" in c for c in self.calls), self.calls)
        self.assertTrue(any("--slurp" not in c for c in self.calls), self.calls)

    def test_other_gh_failure_is_not_retried_without_slurp(self):
        """#2295 : un échec sans rapport avec --slurp garde son diagnostic."""
        def gh(args):
            self.calls.append(args)
            return types.SimpleNamespace(returncode=1, stdout="", stderr="HTTP 403")
        ci = train_ci.GitHub("o/r", (train_ci.Workflow(
            ".github/workflows/tests.yml", ("suite",), "push",
            sources=((".github/workflows/tests.yml", "d" * 40),)),), gh=gh)
        with self.assertRaisesRegex(train.TrainError, "cannot read"):
            ci.read(self.plan)
        self.assertEqual(len(self.calls), 1)

    def test_other_branch_other_head_other_workflow_cannot_vouch(self):
        for key, value in (("head_branch", "other"), ("head_sha", "c" * 40),
                           ("path", ".github/workflows/other.yml"), ("event", "workflow_dispatch")):
            with self.subTest(key=key):
                self.runs = [dict(self.run, **{key: value})]
                self.assertEqual(self.ci().read(self.plan)["status"], "absent")

    def test_workflow_call_binds_the_exact_head_not_the_callers_branch_label(self):
        # #2446: the packet suite is the reusable-workflow run of tests.yml.
        # Its head SHA, path and event are the exact packet identity; GitHub
        # may label the caller ref differently, so the branch label is not a
        # second proof for workflow_call (a push still requires it).
        self.runs = [dict(self.run, event="workflow_call", head_branch="main")]
        self.assertEqual(self.ci("workflow_call").read(self.plan)["status"], "green")
        self.runs = [dict(self.run, event="workflow_call", head_branch="main",
                           head_sha="c" * 40)]
        self.assertEqual(self.ci("workflow_call").read(self.plan)["status"], "absent")

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

    def caller(self, contract, jobs, run=None):
        sources = dict(contract.sources)
        self.runs = [dict(self.run, path=contract.path, event=contract.event,
                          head_branch=self.plan["branch"], head_sha=self.plan["head"],
                          referenced_workflows=[dict(
                              path="o/r/" + contract.called + "@" + self.plan["head"],
                              sha=self.plan["head"], ref="refs/heads/" + self.plan["branch"])])
                     if run is None else run]
        self.jobs = jobs

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
        return train_ci.GitHub("o/r", (contract,), gh=gh)

    def caller_jobs(self, contract, prefix="suite complète du paquet / "):
        # Real API shape of a reusable workflow call (owncloud/core run
        # 39091106232): the jobs list holds `<caller job> / <called job>`
        # (e.g. "PHP Unit Oracle / PHP Unit (8.3, oracle:23)") and no wrapper
        # job. The ameesh caller job is `suite complète du paquet`.
        return [dict(self.job, id=i, name=prefix + name)
                for i, name in enumerate(contract.jobs)]

    def test_ameesh_caller_run_cannot_cover_missing_matrix_part_or_platform(self):
        contract, = train_contracts.ameesh()
        self.assertEqual((contract.path, contract.event, contract.called),
                         (".github/workflows/paquet.yml", "push",
                          ".github/workflows/tests.yml"))
        every = self.caller_jobs(contract)
        ci = self.caller(contract, every)
        self.assertEqual(ci.read(self.plan)["status"], "green")
        # #2446 : the packet suite is the compiled 8 psycopg + 16 psql matrix;
        # a missing caller gate, aggregate, part or platform job can never be
        # replaced by the workflow's green conclusion.
        for missing in ("tests", "psycopg 4/8", "psql 6/16",
                        "plateforme macOS", "plan"):
            with self.subTest(missing=missing):
                actual = next(j["name"] for j in every if j["name"].endswith(missing))
                ci = self.caller(contract, [j for j in every if j["name"] != actual])
                with self.assertRaisesRegex(train.TrainError,
                                            "required full-suite job missing"):
                    ci.read(self.plan)

    def test_covered_packet_push_with_skipped_suite_is_not_ci_evidence(self):
        """Real API shape (run 39117436583): the caller run exists and names
        tests.yml, but its suite job is skipped because an open PR covered the
        head. The `tests (flux) ...` check runs of that PR are not proof."""
        contract, = train_contracts.ameesh()
        jobs = [dict(self.job, id=1, name="couverture (PR ouverte sur cette tête)",
                     conclusion="success"),
                dict(self.job, id=2, name="suite complète du paquet",
                     conclusion="skipped")]
        jobs += [dict(self.job, id=10 + i, name="tests (flux) " + name,
                      conclusion="success")
                 for i, name in enumerate(contract.jobs[1:])]
        ci = self.caller(contract, jobs)
        with self.assertRaisesRegex(train.TrainError, "skipped or interrupted"):
            ci.read(self.plan)

    def test_caller_must_reference_the_audited_suite_at_the_exact_head(self):
        contract, = train_contracts.ameesh()
        every = self.caller_jobs(contract)
        missing = dict(self.run, path=contract.path, event=contract.event,
                       head_branch=self.plan["branch"], head_sha=self.plan["head"])
        ci = self.caller(contract, every, run=missing)
        with self.assertRaisesRegex(train.TrainError, "audited reusable workflow"):
            ci.read(self.plan)
        wrong = dict(missing, referenced_workflows=[dict(
            path="o/r/" + contract.called + "@" + "c" * 40,
            sha="c" * 40, ref="refs/heads/" + self.plan["branch"])])
        ci = self.caller(contract, every, run=wrong)
        with self.assertRaisesRegex(train.TrainError, "exact head"):
            ci.read(self.plan)

    def test_recorded_real_reusable_caller_shape_is_read(self):
        """Shape of owncloud/core run 39002524630: a push caller run names a
        local reusable workflow at the exact head and exposes its job as
        `<caller job> / <called job>`."""
        workflow = train_ci.Workflow(
            path=".github/workflows/ci.yml", jobs=("PHP Unit (8.3, mysql:8.0)",),
            event="push", called=".github/workflows/php-unit.yml",
            sources=((".github/workflows/ci.yml", "d" * 40),
                     (".github/workflows/php-unit.yml", "d" * 40)))
        head = self.plan["head"]
        self.runs = [dict(self.run, path=workflow.path, event="push",
                          head_branch=self.plan["branch"], head_sha=head,
                          referenced_workflows=[dict(
                              path="owncloud/core/.github/workflows/php-unit.yml@" + head,
                              sha=head, ref="refs/heads/master")])]
        self.jobs = [dict(self.job, id=1,
                          name="PHP Unit / PHP Unit (8.3, mysql:8.0)",
                          conclusion="success")]
        ci = train_ci.GitHub("o/r", (workflow,), gh=self.gh)
        self.assertEqual(ci.read(self.plan)["status"], "green")

    def test_unprefixed_called_jobs_are_accepted_and_ambiguous_names_refused(self):
        contract, = train_contracts.ameesh()
        every = self.caller_jobs(contract, prefix="")
        ci = self.caller(contract, every)
        self.assertEqual(ci.read(self.plan)["status"], "green")
        # Two jobs at the same priority would make the proof ambiguous:
        # refuse instead of choosing one.
        prefixed = self.caller_jobs(contract)
        duplicated = prefixed + [dict(self.job, id=999,
                                      name="suite complète du paquet / psycopg 4/8")]
        ci = self.caller(contract, duplicated)
        with self.assertRaisesRegex(train.TrainError, "missing, skipped or interrupted"):
            ci.read(self.plan)
