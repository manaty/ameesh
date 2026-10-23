# SPDX-License-Identifier: AGPL-3.0-only
"""#3244: tests=success certifies a run despite unfinished optional jobs."""
from __future__ import annotations

import unittest
from unittest import mock

from ameesh import plan_github, release_cutoff as release
from . import test_2448_release_cutoff as fixture


def suite(*, mac="cancelled", tests="success", run_id=600):
    def job(name, conclusion):
        return fixture._job(name, conclusion or "", "completed" if conclusion else "in_progress")
    return {"runs": [fixture._run(run_id, [
        job("plan", "success"), job("psql 1/16", "success"),
        job("tests", tests), job("plateforme macOS", mac),
    ], conclusion="failure" if mac == "failure" else (mac or ""))]}


def rollup():
    # The run-level FAILURE may be induced by a non-conclusive job. Only
    # the job read can distinguish that from a test failure.
    return {"green": False, "acceptable": False, "runs": 1,
            "negative": ["tests"], "pending": [], "failed": ["tests"],
            "inconclusive": [], "missing": []}


class OptionalJobReadingTest(unittest.TestCase):
    project = release.ProjectRelease("ameesh", "/unused", github="acme/project")

    def read(self, data, reading=None):
        with mock.patch.object(release.radar, "commit_ci", return_value=reading or rollup()), \
             mock.patch.object(release, "suite_reading", return_value=data):
            return release.ci_reading(self.project, "a" * 40)

    def test_non_conclusive_optional_jobs_are_candidates_and_consigned(self):
        for conclusion in ("cancelled", None, "timed_out", "startup_failure"):
            with self.subTest(conclusion=conclusion):
                reading = self.read(suite(mac=conclusion))
                self.assertTrue(release.reading_is_green(reading), reading)
                self.assertTrue(any("plateforme macOS" in text and "actions/runs/600" in text
                                    for text in reading["inconclusive"]))

    def test_optional_failure_excludes_the_candidate(self):
        reading = self.read(suite(mac="failure"))
        self.assertFalse(release.reading_is_green(reading))
        self.assertTrue(any("plateforme macOS" in text for text in reading["negative"]))

    def test_required_aggregate_must_complete_successfully(self):
        for conclusion in ("cancelled", None, "timed_out", "startup_failure",
                           "skipped", "neutral", "failure"):
            with self.subTest(conclusion=conclusion):
                reading = self.read(suite(tests=conclusion), {"green": True})
                self.assertFalse(release.reading_is_green(reading), reading)

    def test_missing_aggregate_and_empty_run_never_certify(self):
        data = suite()
        data["runs"][0]["jobs"] = [fixture._job("plateforme macOS")]
        self.assertFalse(self.read(data, {"green": True})["green"])
        self.assertFalse(self.read({"runs": []}, {"green": True})["green"])

    def test_older_green_run_cannot_hide_newer_failure(self):
        data = suite(mac="failure", run_id=601)
        data["runs"].extend(suite(mac="success")["runs"])
        self.assertFalse(self.read(data)["green"])

    def test_other_negative_checks_still_block(self):
        reading = rollup()
        reading["negative"].append("security")
        self.assertFalse(self.read(suite(), reading)["green"])

    def test_unreadable_jobs_are_transient(self):
        with mock.patch.object(release.radar, "commit_ci", return_value=rollup()), \
             mock.patch.object(release.radar, "commit_suite",
                               side_effect=plan_github.GithubError("offline")):
            with self.assertRaises(release.TransientReleaseError):
                release.ci_reading(self.project, "a" * 40)

    def test_tree_proof_accepts_optional_non_conclusions_but_not_failure(self):
        reference = suite(mac="success")
        for conclusion in ("cancelled", None, "timed_out", "startup_failure"):
            with self.subTest(conclusion=conclusion):
                candidate = suite(mac=conclusion)
                self.assertIsNotNone(release._full_suite_run(candidate))
                self.assertIsNotNone(release._suite_proof(reference, candidate))
                self.assertIsNotNone(release._suite_proof(candidate, reference))
        self.assertIsNone(release._suite_proof(reference, suite(mac="failure")))
        self.assertIsNone(release._suite_proof(reference, suite(tests="cancelled")))
        # A job not green in the reference must not hide a candidate failure.
        self.assertIsNone(release._suite_proof(suite(), suite(mac="failure")))


class OptionalJobPlanTest(fixture.ReleaseFixtureMixin, unittest.TestCase):
    def plan(self, *, mac="cancelled", tests="success"):
        def jobs(project, sha):
            return suite(mac=mac, tests=tests) if sha == self.head else suite(mac="success")
        with mock.patch.object(release.radar, "commit_ci", return_value=rollup()), \
             mock.patch.object(release, "suite_reading", side_effect=jobs):
            return release.build_plan(self.cfg, self.project, now=fixture._paris(11, 5))

    def test_head_is_selected_and_optional_job_is_in_release_notes(self):
        for conclusion in ("cancelled", None, "timed_out", "startup_failure"):
            with self.subTest(conclusion=conclusion):
                plan = self.plan(mac=conclusion)
                self.assertEqual(plan.base, self.head)
                self.assertTrue(plan.green)
                self.assertIn("plateforme macOS", plan.notes)
                self.assertIn(conclusion or "in_progress", plan.notes)
                self.assertIn("actions/runs/600", plan.notes)

    def test_optional_failure_falls_back_to_previous_green_commit(self):
        plan = self.plan(mac="failure")
        self.assertFalse(plan.green)
        self.assertNotEqual(plan.base, self.head)

    def test_non_conclusive_aggregate_falls_back_to_previous_green_commit(self):
        plan = self.plan(tests="cancelled")
        self.assertFalse(plan.green)
        self.assertNotEqual(plan.base, self.head)

    def test_inherited_packet_proof_records_optional_job_in_notes(self):
        packet = self.git(self.seed, "rev-parse", self.head + "^2")

        def jobs(project, sha):
            if sha == self.head:
                return suite(tests=None)
            if sha == packet:
                return suite(mac=None)
            return suite(mac="success", run_id=500)

        with mock.patch.object(release.radar, "commit_ci", return_value={
                **rollup(), "negative": [], "pending": ["tests"], "failed": []}), \
             mock.patch.object(release, "suite_reading", side_effect=jobs):
            plan = release.build_plan(self.cfg, self.project, now=fixture._paris(11, 5))
        self.assertEqual(plan.base, self.head)
        self.assertEqual(plan.tree_green["head"], packet)
        self.assertIn("plateforme macOS (in_progress)", plan.notes)

    def test_fallback_base_keeps_its_optional_job_in_release_notes(self):
        self.write("red.txt", "red\n")
        red = self.commit("new main with a failed optional job")
        self.git(self.seed, "push", "-q", "origin", "main")

        def jobs(project, sha):
            return suite(mac="failure" if sha == red else "cancelled")

        with mock.patch.object(release.radar, "commit_ci", return_value=rollup()), \
             mock.patch.object(release, "suite_reading", side_effect=jobs):
            plan = release.build_plan(self.cfg, self.project, now=fixture._paris(11, 5))
        self.assertEqual(plan.base, self.head)
        self.assertFalse(plan.green)
        self.assertIn(self.head[:12], plan.notes)
        self.assertIn("plateforme macOS (cancelled", plan.notes)
