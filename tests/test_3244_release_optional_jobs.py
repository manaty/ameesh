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


#: #3186 : `ci_reading` lit aussi les checks requis de la cible à la forge ;
#: ici, aucun (les agrégats jamais conclus ont leurs témoins dans test_3186).
_REQUIRED = mock.patch.object(release, "required_checks", return_value=())


def setUpModule():
    _REQUIRED.start()


def tearDownModule():
    _REQUIRED.stop()


class OptionalJobReadingTest(unittest.TestCase):
    project = release.ProjectRelease("ameesh", "/unused", github="acme/project")

    def read(self, data, reading=None):
        with mock.patch.object(release.radar, "commit_ci", return_value=reading or rollup()), \
             mock.patch.object(release, "suite_reading", return_value=data):
            return release.ci_reading(self.project, "a" * 40)

    def test_non_conclusive_optional_jobs_are_candidates_and_consigned(self):
        for conclusion in ("cancelled", "timed_out", "startup_failure"):
            with self.subTest(conclusion=conclusion):
                reading = self.read(suite(mac=conclusion))
                self.assertTrue(release.reading_is_green(reading), reading)
                self.assertTrue(any("plateforme macOS" in text and "actions/runs/600" in text
                                    for text in reading["inconclusive"]))

    def test_an_in_progress_optional_job_waits_and_is_not_consigned(self):
        # #3530 : une part encore en cours n'a pas conclu ; elle attend, elle
        # n'ouvre pas de lot de correction.
        reading = self.read(suite(mac=None))
        self.assertTrue(release.reading_is_green(reading), reading)
        self.assertEqual(reading["inconclusive"], [])
        self.assertEqual(release._run_evidence(suite(mac=None)["runs"][0])["inconclusive"],
                         [])

    def test_jobs_skipped_by_design_are_not_consigned(self):
        # Real push run on main: these jobs are skipped by their `if:`.
        data = suite(mac="success")
        data["runs"][0]["jobs"] += [fixture._job("version number", "skipped"),
                                    fixture._job("lecteurs de la doc", "skipped")]
        reading = self.read(data)
        self.assertTrue(release.reading_is_green(reading), reading)
        self.assertEqual(reading["inconclusive"], [])
        self.assertEqual(release._run_evidence(data["runs"][0])["inconclusive"], [])

    def test_separate_macos_workflow_is_consigned_without_blocking(self):
        for conclusion in ("cancelled", "timed_out", "startup_failure"):
            with self.subTest(conclusion=conclusion):
                data = suite(mac="success")
                data["runs"].append(fixture._run(700, [fixture._job(
                    "suite macOS complète", conclusion,
                    "completed")],
                    workflow="macos", conclusion=conclusion))
                reading = self.read(data, {
                    **rollup(), "negative": [], "failed": [],
                    "pending": [], "inconclusive": ["macos"],
                })
                self.assertTrue(reading["green"], reading)
                self.assertEqual(reading["pending"], [])
                self.assertEqual(reading["inconclusive"], [
                    "suite macOS complète (%s; https://github.com/manaty/ameesh/actions/runs/700)"
                    % conclusion])
                self.assertEqual(release._full_suite_run(data)["id"], 600)

    def test_a_running_separate_macos_workflow_waits_without_blocking(self):
        # #3530 : le workflow facultatif encore en cours n'est pas consigné et
        # ne retient pas l'agrégat vert.
        data = suite(mac="success")
        data["runs"].append(fixture._run(700, [fixture._job(
            "suite macOS complète", "", "in_progress")],
            workflow="macos", conclusion=""))
        reading = self.read(data, {
            **rollup(), "negative": [], "failed": [], "pending": ["macos"],
        })
        self.assertTrue(reading["green"], reading)
        self.assertEqual(reading["pending"], [])
        self.assertEqual(reading["inconclusive"], [])
        self.assertEqual(release._full_suite_run(data)["id"], 600)

    def test_separate_macos_workflow_failure_still_blocks(self):
        data = suite(mac="success")
        data["runs"].append(fixture._run(700, [
            fixture._job("suite macOS complète", "failure")],
            workflow="macos", conclusion="failure"))
        reading = self.read(data, {**rollup(), "negative": ["macos"]})
        self.assertFalse(reading["green"], reading)
        self.assertTrue(any("suite macOS complète (failure;" in name
                            for name in reading["negative"]))

    def test_only_a_newer_job_success_clears_an_earlier_macos_failure(self):
        for separate in (True, False):
            for conclusion in (None, "cancelled", "timed_out", "startup_failure",
                               "skipped", "success"):
                data = suite(mac="success")
                if separate:
                    old = fixture._run(700, [fixture._job("suite macOS complète", "failure")],
                                       workflow="macos", conclusion="failure")
                    new = fixture._run(800, [fixture._job(
                        "suite macOS complète", conclusion or "",
                        "completed" if conclusion else "in_progress")],
                        workflow="macos", conclusion=conclusion or "")
                else:
                    old = suite(mac="failure", run_id=700)["runs"][0]
                    new = suite(mac=conclusion, run_id=800)["runs"][0]
                for history in ([old, new], [new, old]):
                    with self.subTest(separate=separate, conclusion=conclusion,
                                      order=[run["id"] for run in history]):
                        candidate = {"runs": data["runs"] + history}
                        reading = self.read(candidate, {**rollup(), "negative": []})
                        repaired = conclusion == "success"
                        self.assertEqual(reading["green"], repaired, reading)
                        self.assertEqual(release._suite_proof(data, candidate) is not None,
                                         repaired)
                        self.assertEqual(release._full_suite_run(candidate) is not None, repaired)
                        if not repaired:
                            self.assertIsNone(release._suite_proof(candidate, data))
                            self.assertTrue(any("actions/runs/700" in text
                                                for text in reading["negative"]), reading)

    def test_a_different_successful_job_does_not_clear_the_failure(self):
        data = suite(mac="success")
        data["runs"] += [fixture._run(700, [
            fixture._job("suite macOS complète", "failure"),
            fixture._job("lint macOS", "failure"),
        ], workflow="macos", conclusion="failure"), fixture._run(800, [
            fixture._job("suite macOS complète"),
            fixture._job("lint macOS", "cancelled"),
        ], workflow="macos", conclusion="cancelled")]
        reading = self.read(data)
        self.assertEqual(reading["negative"], [
            "lint macOS (failure; https://github.com/manaty/ameesh/actions/runs/700)"])
        self.assertIsNone(release._suite_proof(suite(mac="success"), data))

    def test_a_successful_repair_is_not_undone_by_a_later_non_conclusion(self):
        data = suite(mac="success")
        data["runs"] += [fixture._run(run_id, [fixture._job(
            "suite macOS complète", conclusion)], workflow="macos", conclusion=conclusion)
            for run_id, conclusion in ((700, "failure"), (750, "success"), (800, "cancelled"))]
        reading = self.read(data)
        self.assertTrue(reading["green"], reading)
        self.assertEqual(reading["negative"], [])
        self.assertIsNotNone(release._suite_proof(suite(mac="success"), data))

    def test_a_newer_packet_suite_can_repair_the_same_full_suite_jobs(self):
        data = suite(mac="failure", tests="failure", run_id=700)
        new = suite(mac="success", run_id=800)["runs"][0]
        new["workflow"] = "paquet"
        for job in new["jobs"]:
            job["name"] = "suite complète du paquet / " + job["name"]
        data["runs"].append(new)
        self.assertTrue(self.read(data)["green"])
        self.assertEqual(release._full_suite_run(data)["id"], 800)
        self.assertIsNotNone(release._suite_proof(suite(mac="success"), data))

    def test_separate_macos_workflow_never_certifies_a_non_conclusive_aggregate(self):
        data = suite(mac="success", tests="cancelled")
        data["runs"].append(fixture._run(700, [fixture._job(
            "suite macOS complète", "", "in_progress")],
            workflow="macos", conclusion=""))
        reading = self.read(data, {
            **rollup(), "negative": [], "pending": ["macos"],
        })
        self.assertFalse(reading["green"], reading)
        self.assertTrue(any("agrégat tests non validé" in name for name in reading["pending"]))

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
        self.assertIsNone(release._full_suite_run(data))
        self.assertIsNone(release._suite_proof(suite(mac="success"), data))

    def test_manual_dispatch_aggregate_certifies_the_latest_suite(self):
        data = suite(mac="success", run_id=601)
        data["runs"][0]["event"] = "workflow_dispatch"
        data["runs"][0]["jobs"][2]["name"] = "tests (dispatch)"
        data["runs"].extend(suite(mac="success")["runs"])
        self.assertTrue(self.read(data)["green"])
        self.assertEqual(release._full_suite_run(data)["id"], 601)
        self.assertIsNotNone(release._suite_proof(suite(mac="success"), data))
        self.assertIsNotNone(release._suite_proof(data, suite(mac="success")))

    def test_skipped_macos_label_run_does_not_replace_the_suite(self):
        data = suite(mac="success")
        data["runs"].append(fixture._run(601, [], event="pull_request",
                                          conclusion="skipped"))
        self.assertTrue(self.read(data)["green"])
        self.assertEqual(release._full_suite_run(data)["id"], 600)
        self.assertIsNotNone(release._suite_proof(suite(mac="success"), data))

    def test_packet_porter_can_carry_the_only_full_suite(self):
        data = suite(mac="success")
        data["runs"][0]["workflow"] = "paquet"
        for job in data["runs"][0]["jobs"]:
            job["name"] = "suite complète du paquet / " + job["name"]
        reading = self.read(data, {**rollup(), "negative": ["paquet"]})
        self.assertTrue(reading["green"], reading)
        self.assertIsNotNone(release._full_suite_run(data))
        self.assertIsNotNone(release._suite_proof(suite(mac="success"), data))
        self.assertIsNotNone(release._suite_proof(data, suite(mac="success")))

    def test_coverage_only_porter_does_not_replace_the_tests_suite(self):
        data = suite(mac="success")
        data["runs"].append(fixture._run(601, [
            fixture._job("couverture (PR ouverte sur cette tête)"),
            fixture._job("suite complète du paquet", "skipped"),
        ], workflow="paquet"))
        self.assertTrue(self.read(data)["green"])
        self.assertEqual(release._full_suite_run(data)["id"], 600)

    def test_an_aborted_or_queued_suite_never_falls_back_to_old_green(self):
        for conclusion in ("failure", "cancelled", "startup_failure", ""):
            with self.subTest(conclusion=conclusion):
                data = suite(mac="success")
                data["runs"].append(fixture._run(601, [], conclusion=conclusion))
                reading = self.read(data, {"green": True})
                self.assertFalse(reading["green"], reading)
                self.assertTrue(reading["pending"], reading)
                self.assertIsNone(release._full_suite_run(data))
                self.assertIsNone(release._suite_proof(suite(mac="success"), data))

    def test_a_newer_failed_dispatch_supersedes_the_green_push(self):
        data = suite(mac="success", tests="failure", run_id=601)
        data["runs"][0]["event"] = "workflow_dispatch"
        data["runs"][0]["jobs"][2]["name"] = "tests (dispatch)"
        data["runs"][0]["conclusion"] = "failure"
        data["runs"].extend(suite(mac="success")["runs"])
        self.assertFalse(self.read(data)["green"])
        self.assertIsNone(release._full_suite_run(data))

    def test_aborted_packet_suite_without_aggregate_supersedes_old_green(self):
        data = suite(mac="success")
        data["runs"].append(fixture._run(601, [fixture._job(
            "suite complète du paquet / plan", "failure")],
            workflow="paquet", conclusion="failure"))
        self.assertFalse(self.read(data)["green"])
        self.assertIsNone(release._full_suite_run(data))
        self.assertIsNone(release._suite_proof(suite(mac="success"), data))

    def test_a_declared_tolerated_failure_neither_certifies_nor_blocks(self):
        data = suite(mac="success")
        bilan = {**fixture._job("bilan des impactés", "failure"), "tolerated": True}
        data["runs"][0]["jobs"].append(bilan)
        self.assertTrue(self.read(data)["green"])
        self.assertIsNotNone(release._full_suite_run(data))
        self.assertIsNotNone(release._suite_proof(suite(mac="success"), data))
        self.assertEqual(release._run_evidence(data["runs"][0])["inconclusive"], [])
        bilan["conclusion"] = "success"
        self.assertIsNotNone(release._suite_proof(data, suite(mac="success")))
        bilan["conclusion"] = "failure"
        bilan["tolerated"] = False
        self.assertFalse(self.read(data)["green"])
        self.assertIsNone(release._full_suite_run(data))

    def test_tolerances_are_read_from_the_existing_radar_configuration(self):
        cfg = mock.Mock(humans="smichea", release={}, packet_watch={},
                        radar={"projects": {"ameesh": {
            "repo": "/unused", "github": "acme/project", "full_suite": [{
                "path": ".github/workflows/tests.yml", "jobs": ["tests"],
                "tolerate": ["bilan des impactés"],
            }],
        }}})
        project = release.ProjectRelease.from_config(cfg, "ameesh", {"repo": "/unused"})
        self.assertEqual(project.suite_tolerated, ("bilan des impactés",))
        data = suite(mac="success")
        run = data["runs"][0]
        run["workflow"] = "paquet"
        run["jobs"].append(fixture._job(
            "suite complète du paquet / bilan des impactés", "failure"))
        with mock.patch.object(release.radar, "commit_suite", return_value=data):
            reading = release.suite_reading(project, "a" * 40)
        self.assertTrue(reading["runs"][0]["jobs"][-1]["tolerated"])
        self.assertFalse(reading["runs"][0]["jobs"][2]["tolerated"])
        self.assertIsNotNone(release._full_suite_run(reading))

    def test_latest_attempt_cannot_borrow_jobs_from_a_superseded_suite(self):
        data = suite(mac="success", run_id=601)
        data["runs"][0]["jobs"] = [fixture._job("tests (dispatch)")]
        data["runs"].extend(suite(mac="success")["runs"])
        self.assertIsNone(release._suite_proof(suite(mac="success"), data))

    def test_other_negative_checks_still_block(self):
        reading = rollup()
        reading["negative"].append("security")
        self.assertFalse(self.read(suite(), reading)["green"])

    def test_an_unconcluded_required_check_is_never_acceptable(self):
        # #3186 × #3244 : un requis jamais conclu, rendu par la lecture des
        # checks requis, reste une preuve absente même si `tests` a conclu.
        reading = rollup()
        reading["unconcluded"] = [{"check": "security", "databaseId": 700}]
        result = self.read(suite(mac="success"), reading)
        self.assertFalse(result["acceptable"])
        self.assertFalse(release.reading_is_green(result))
        self.assertEqual(result["unconcluded"], reading["unconcluded"])

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
        # #3633 : la tête du paquet (^2) porte le même arbre que la tête ; elle
        # a donc la même suite, sinon son vert ferait hériter la tête (décision
        # owner #141353 a) et le témoin ne lirait plus la CI propre de la tête.
        packet = self.git(self.seed, "rev-parse", self.head + "^2")

        def jobs(project, sha):
            if sha in (self.head, packet):
                return suite(mac=mac, tests=tests)
            return suite(mac="success")
        with mock.patch.object(release.radar, "commit_ci", return_value=rollup()), \
             mock.patch.object(release, "suite_reading", side_effect=jobs):
            return release.build_plan(self.cfg, self.project, now=fixture._paris(11, 5))

    def test_head_is_selected_and_optional_job_is_in_release_notes(self):
        for conclusion in ("cancelled", "timed_out", "startup_failure"):
            with self.subTest(conclusion=conclusion):
                plan = self.plan(mac=conclusion)
                self.assertEqual(plan.base, self.head)
                self.assertTrue(plan.green)
                self.assertIn("plateforme macOS", plan.notes)
                self.assertIn(conclusion, plan.notes)
                self.assertIn("actions/runs/600", plan.notes)

    def test_a_running_optional_job_stays_out_of_the_release_notes(self):
        # #3530 : un job encore en cours n'est pas un non concluant ; il ne
        # s'écrit pas dans les notes et n'ouvre pas de lot.
        plan = self.plan(mac=None)
        self.assertEqual(plan.base, self.head)
        self.assertTrue(plan.green)
        self.assertNotIn("plateforme macOS", plan.notes)

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
                return suite(mac="cancelled")
            return suite(mac="success", run_id=500)

        with mock.patch.object(release.radar, "commit_ci", return_value={
                **rollup(), "negative": [], "pending": ["tests"], "failed": []}), \
             mock.patch.object(release, "suite_reading", side_effect=jobs):
            plan = release.build_plan(self.cfg, self.project, now=fixture._paris(11, 5))
        self.assertEqual(plan.base, self.head)
        self.assertEqual(plan.tree_green["head"], packet)
        self.assertIn("plateforme macOS (cancelled", plan.notes)

    def test_inherited_packet_proof_leaves_a_running_job_out_of_the_notes(self):
        # #3530 : un job encore en cours sur la tête du paquet n'est pas un
        # non concluant à citer.
        packet = self.git(self.seed, "rev-parse", self.head + "^2")

        def jobs(project, sha):
            if sha == self.head:
                return suite(mac=None, tests=None)
            if sha == packet:
                return suite(mac=None)
            return suite(mac="success", run_id=500)

        with mock.patch.object(release.radar, "commit_ci", return_value={
                **rollup(), "negative": [], "pending": ["tests"], "failed": []}), \
             mock.patch.object(release, "suite_reading", side_effect=jobs):
            plan = release.build_plan(self.cfg, self.project, now=fixture._paris(11, 5))
        self.assertEqual(plan.base, self.head)
        self.assertEqual(plan.tree_green["head"], packet)
        self.assertNotIn("plateforme macOS", plan.notes)

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
