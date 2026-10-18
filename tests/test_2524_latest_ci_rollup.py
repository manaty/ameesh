# SPDX-License-Identifier: AGPL-3.0-only
"""#2524: reruns on one head supersede earlier checks, regardless of list order."""
import json
import types
import unittest

from ameesh import fire, land, radar

OLD = "2026-10-18T17:22:28Z"
NEW = "2026-10-18T17:34:39Z"
SHA = "a" * 40


def check(conclusion="SUCCESS", *, started=NEW, workflow="tests", status="COMPLETED"):
    return {"__typename": "CheckRun", "name": "tests", "workflowName": workflow,
            "startedAt": started, "status": status, "conclusion": conclusion}


def context(state="SUCCESS", *, created=NEW, field="startedAt"):
    # `gh pr list --json statusCheckRollup` names a status's date `startedAt`.
    return {"__typename": "StatusContext", "context": "approval/team",
            "state": state, field: created}


def gh_for(rollup):
    def gh(argv, **kwargs):
        return types.SimpleNamespace(returncode=0, stderr="", stdout=json.dumps([
            {"number": 1870, "state": "OPEN", "headRefOid": SHA,
             "baseRefName": "main", "statusCheckRollup": rollup}]))
    return gh


class LatestCiTest(unittest.TestCase):
    def assert_readers(self, rollup, expected):
        self.assertEqual(radar.ci_green(rollup), expected == "green")
        for reader in (fire._pr_status, land.pr_status):
            with self.subTest(reader=reader.__module__):
                self.assertEqual(reader("acme/example", "lot/2524", gh=gh_for(rollup))["ci"],
                                 expected)

    def test_latest_result_in_either_list_order(self):
        for conclusion, status, expected in (("SUCCESS", "COMPLETED", "green"),
                                              ("FAILURE", "COMPLETED", "red"),
                                              ("", "IN_PROGRESS", "pending"),
                                              ("", "QUEUED", "pending")):
            for old in ("FAILURE", "SUCCESS"):
                rows = [check(old, started=OLD), check(conclusion, status=status)]
                for rollup in (rows, rows[::-1]):
                    with self.subTest(conclusion=conclusion, status=status, old=old):
                        self.assert_readers(rollup, expected)
                        status_row = fire._pr_status("acme/example", "lot/2524",
                                                     gh=gh_for(rollup))
                        ready, _ = fire.ready_pr(status_row, target="main", project=None,
                                                 frozen=SHA, required=("tests",))
                        self.assertEqual(ready, expected == "green")

    def test_obsolete_pending_does_not_hide_latest_failure(self):
        self.assert_readers([check("", started=OLD, status="IN_PROGRESS"),
                             check("FAILURE")], "red")

    def test_status_contexts_use_their_date(self):
        cases = (("SUCCESS", "green"), ("FAILURE", "red"), ("PENDING", "pending"))
        for field in ("startedAt", "createdAt"):
            for state, expected in cases:
                rows = [context("FAILURE", created=OLD, field=field),
                        context(state, field=field)]
                for rollup in (rows, rows[::-1]):
                    with self.subTest(field=field, state=state):
                        self.assert_readers(rollup, expected)
                        self.assertEqual(radar.ci_green(rollup, required=("approval/",)),
                                         expected == "green")
                        self.assertEqual(bool(fire._required_gap(("approval/team",), rollup)),
                                         expected != "green")

    def test_distinct_workflows_and_contexts_do_not_replace_each_other(self):
        self.assert_readers([check("FAILURE", started=OLD, workflow="security"),
                             check()], "red")
        rows = [check("FAILURE", started=OLD),
                dict(context(), context="tests")]
        self.assert_readers(rows, "red")
        self.assertTrue(fire._required_gap(("tests",), rows))

    def test_ambiguous_dates_keep_failures(self):
        for date in (None, "", "invalid", "2026-10-18T17:22:28", NEW):
            rows = [check("FAILURE", started=date), check()]
            for rollup in (rows, rows[::-1]):
                with self.subTest(date=date):
                    self.assert_readers(rollup, "red")
                    self.assertTrue(fire._required_gap(("tests",), rollup))

    def test_timestamp_offsets_are_compared_as_instants(self):
        self.assert_readers([check("FAILURE", started="2026-10-18T20:00:00+03:00"),
                             check()], "green")

    def test_required_checks_filter_direct_rollup_too(self):
        rows = [check("FAILURE", started=OLD), check()]
        status = {"number": 1870, "ci": "green", "base": "main", "head": SHA,
                  "rollup": rows}
        self.assertTrue(fire.ready_pr(status, target="main", project=None,
                                      frozen=SHA, required=("tests",))[0])
        self.assertTrue(fire._required_gap(("tests",), [check("SUCCESS", started=OLD),
                                                       check("SKIPPED")]))

    def test_empty_and_unidentified_checks_keep_existing_behavior(self):
        self.assert_readers([], "pending")
        rows = [{"status": "COMPLETED", "conclusion": "FAILURE"}, check()]
        self.assert_readers(rows, "red")


if __name__ == "__main__":
    unittest.main()
