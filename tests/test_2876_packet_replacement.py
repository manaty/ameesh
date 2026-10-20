# SPDX-License-Identifier: AGPL-3.0-only
"""#2876 : une suite PR verte remplace le portier annulé de la même tête."""
import copy
import json
import types
import unittest

from ameesh import fire, land, merge_pr, packet_automerge, radar


SHA = "a" * 40
OLD = "2026-10-20T11:37:57Z"
NEW = "2026-10-20T12:20:00Z"


def check(name, conclusion="SUCCESS", *, workflow="tests", end=NEW):
    return {"__typename": "CheckRun", "name": name, "workflowName": workflow,
            "status": "COMPLETED", "conclusion": conclusion,
            "startedAt": "2026-10-20T11:17:30Z", "completedAt": end}


def witness():
    # Noms et conclusions du portier de #2194, run 39413892894 : des parts
    # CANCELLED et un agrégat FAILURE restent dans le rollup. Le résultat PR
    # SUCCESS ci-dessous est le scénario de remplacement, pas un verdict sur
    # le run réel 39414426281 (encore rouge/en cours au relevé).
    return [check("couverture (PR ouverte sur cette tête)", workflow="paquet", end=OLD),
            check("suite complète du paquet / psql 2/16", "CANCELLED",
                  workflow="paquet", end=OLD),
            check("suite complète du paquet / tests", "FAILURE",
                  workflow="paquet", end="2026-10-20T11:54:26Z"),
            check("psql 2/16"), check("tests"), check("ui", workflow="ui")]


def view(rows):
    return {"number": 2194, "state": "OPEN", "headRefOid": SHA,
            "headRefName": "integration/paquet-h-2876", "baseRefName": "main",
            "mergeStateStatus": "CLEAN", "statusCheckRollup": rows}


def gh_for(rows, *, single=False):
    def gh(argv, **kwargs):
        payload = view(rows)
        return types.SimpleNamespace(returncode=0, stderr="",
                                     stdout=json.dumps(payload if single else [payload]))
    return gh


class PacketReplacementTest(unittest.TestCase):
    def assert_gate(self, rows, green):
        self.assertEqual(radar.ci_green(rows), green)
        for reader in (fire._pr_status, land.pr_status):
            status = reader("manaty/ameesh", "integration/paquet-h-2876", gh=gh_for(rows))
            self.assertEqual(status["ci"] == "green", green)
            if reader is fire._pr_status:
                ready, why = fire.ready_pr(status, target="main", project=None,
                                          frozen=SHA, required=("tests",))
                self.assertEqual(ready, green, why)
        status = packet_automerge.pr_status("manaty/ameesh", 2194,
                                           gh=gh_for(rows, single=True))
        self.assertEqual(status["ci"] == "green", green)
        ready, why = fire.ready_pr(status, target="main", project=None,
                                  frozen=SHA, required=("tests",))
        self.assertEqual(ready, green, why)
        if green:
            merge_pr._pr_guards(view(rows), 2194, SHA, "main")
        else:
            with self.assertRaises(merge_pr.MergePrError):
                merge_pr._pr_guards(view(rows), 2194, SHA, "main")

    def test_cancelled_packet_and_failed_aggregate_replaced_by_green_pr(self):
        rows = witness()
        for ordered in (rows, rows[::-1]):
            self.assert_gate(ordered, True)

    def test_missing_skipped_failed_or_pending_pr_never_replaces_packet(self):
        rows = witness()
        self.assert_gate([c for c in rows if c["name"] != "tests"], False)
        for conclusion, status in (("SKIPPED", "COMPLETED"), ("FAILURE", "COMPLETED"),
                                   ("", "IN_PROGRESS"), ("", "QUEUED")):
            rows = witness()
            rows[4].update(conclusion=conclusion, status=status)
            self.assert_gate(rows, False)

    def test_dates_must_prove_replacement_finished_after_packet(self):
        for index in (1, 4):
            for value in (None, "", "invalid", "2026-10-20T12:20:00"):
                rows = witness()
                rows[index]["completedAt"] = value
                self.assert_gate(rows, False)
        for end in (OLD, "2026-10-20T11:54:26Z"):
            rows = witness()
            rows[4]["completedAt"] = end
            self.assert_gate(rows, False)

    def test_still_running_packet_is_not_ignored(self):
        rows = witness()
        rows[2].update(status="IN_PROGRESS", conclusion="", completedAt=None)
        self.assert_gate(rows, False)

    def test_packet_failure_without_cancellation_is_not_ignored(self):
        rows = witness()
        rows[1]["conclusion"] = "FAILURE"
        self.assert_gate(rows, False)

    def test_other_failures_and_unknown_packet_jobs_remain_blocking(self):
        for workflow, name in (("security", "tests"), ("ui", "ui"),
                               ("tests", "psql 2/16"), ("paquet", "security")):
            rows = witness() + [check(name, "FAILURE", workflow=workflow)]
            self.assert_gate(rows, False)

    def test_status_contexts_cannot_replace_pr_or_be_erased(self):
        rows = witness()
        rows[4] = {"__typename": "StatusContext", "context": "tests", "state": "SUCCESS"}
        self.assert_gate(rows, False)
        self.assert_gate(witness() + [dict(rows[4], state="FAILURE")], False)
        self.assertFalse(radar.ci_green(witness(), required=("approval/",)))

    def test_wrong_workflow_and_ambiguous_aggregates_do_not_replace_packet(self):
        rows = witness()
        rows[4]["workflowName"] = "other"
        self.assert_gate(rows, False)
        rows = witness()
        self.assert_gate(rows + [copy.deepcopy(rows[4])], False)

    def test_timestamp_offsets_are_compared_as_instants(self):
        rows = witness()
        rows[4]["completedAt"] = "2026-10-20T14:20:00+02:00"
        self.assert_gate(rows, True)


if __name__ == "__main__":
    unittest.main()
