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


def check(name, conclusion="SUCCESS", *, workflow="tests", end=NEW, run=None):
    run = run or (100 if workflow == "paquet" else 200)
    return {"detailsUrl": f"https://github.com/manaty/ameesh/actions/runs/{run}/job/123",
            "__typename": "CheckRun", "name": name, "workflowName": workflow,
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


def packet_run(id=100, *, status="completed", conclusion="cancelled"):
    return {"id": id, "head_sha": SHA, "head_branch": "integration/paquet-h-2876",
            "event": "push", "path": ".github/workflows/paquet.yml",
            "status": status, "conclusion": conclusion}


def gh_for(rows, *, single=False, runs=None, pr_run=None, api_error=False):
    def gh(argv, **kwargs):
        if argv[0] == "api":
            if api_error:
                return types.SimpleNamespace(returncode=1, stderr="unavailable", stdout="")
            if "/actions/runs?" in argv[1]:
                from urllib.parse import parse_qs, urlsplit
                page = int(parse_qs(urlsplit(argv[1]).query)["page"][0])
                listed = [packet_run()] if runs is None else runs
                payload = {"workflow_runs": listed[(page-1)*100:page*100],
                           "total_count": len(listed)}
            else:
                payload = pr_run if pr_run is not None else {
                    **packet_run(200, conclusion="success"), "event": "pull_request",
                    "path": ".github/workflows/tests.yml"}
        else:
            payload = view(rows)
            if not single:
                payload = [payload]
        return types.SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(payload))
    return gh


class PacketReplacementTest(unittest.TestCase):
    def assert_gate(self, rows, green, *, gate=None, **proof):
        """`green` est la preuve stricte (`ci_green`, `status["ci"]`, merge-pr) ;
        `gate` est le verdict du feu/paquet (#3085), qui peut absoudre un non
        concluant hors du portier `paquet`."""
        gate = green if gate is None else gate
        normalized = radar.merge_rollup(rows, slug="manaty/ameesh", head=SHA,
                                        branch="integration/paquet-h-2876",
                                        gh=gh_for(rows, **proof))
        self.assertEqual(radar.ci_green(normalized), green)
        for reader in (fire._pr_status, land.pr_status):
            status = reader("manaty/ameesh", "integration/paquet-h-2876", gh=gh_for(rows, **proof))
            self.assertEqual(status["ci"] == "green", green)
            if reader is fire._pr_status:
                ready, why = fire.ready_pr(status, target="main", project=None,
                                          frozen=SHA, required=("tests",))
                self.assertEqual(ready, gate, why)
        status = packet_automerge.pr_status("manaty/ameesh", 2194,
                                           gh=gh_for(rows, single=True, **proof))
        self.assertEqual(status["ci"] == "green", green)
        ready, why = fire.ready_pr(status, target="main", project=None,
                                  frozen=SHA, required=("tests",))
        self.assertEqual(ready, gate, why)
        merge_view = merge_pr.pr_view("manaty/ameesh", 2194,
                                       gh=gh_for(rows, single=True, **proof))
        if green:
            merge_pr._pr_guards(merge_view, 2194, SHA, "main")
        else:
            with self.assertRaises(merge_pr.MergePrError):
                merge_pr._pr_guards(merge_view, 2194, SHA, "main")

    def test_cancelled_packet_and_failed_aggregate_replaced_by_green_pr(self):
        rows = witness()
        for ordered in (rows, rows[::-1]):
            self.assert_gate(ordered, True)

    def test_missing_skipped_failed_or_pending_pr_never_replaces_packet(self):
        rows = witness()
        self.assert_gate([c for c in rows if c["name"] != "tests"], False)
        for conclusion, status in (("SKIPPED", "COMPLETED"), ("NEUTRAL", "COMPLETED"),
                                   ("FAILURE", "COMPLETED"),
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
        self.assert_gate(rows, False, runs=[packet_run(conclusion="failure")])

    def test_other_failures_and_unknown_packet_jobs_remain_blocking(self):
        for workflow, name in (("security", "tests"), ("ui", "ui"),
                               ("tests", "psql 2/16"), ("paquet", "security")):
            for conclusion in ("FAILURE", "CANCELLED"):
                rows = witness() + [check(name, conclusion, workflow=workflow)]
                # #3085/#3066 : le feu/paquet ne lit que les checks requis et
                # le portier `paquet`. Un check facultatif (autre nom, autre
                # workflow) ne gate pas, même rouge ; un `FAILURE` du nom
                # requis ou du portier bloque encore, et un CANCELLED du
                # portier aussi (preuve stricte). `ci_green`, `status.ci` et
                # merge-pr gardent leur preuve stricte.
                blocking = (workflow == "paquet"
                            or (name == "tests" and conclusion == "FAILURE"))
                with self.subTest(workflow=workflow, name=name, conclusion=conclusion):
                    self.assert_gate(rows, False, gate=not blocking)

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

    def test_old_cancellation_never_hides_newer_failed_or_active_run(self):
        for status, conclusion in (("completed", "failure"), ("in_progress", None),
                                   ("queued", None)):
            runs = [packet_run(), packet_run(101, status=status, conclusion=conclusion)]
            rows = witness() + [check("suite complète du paquet / plan", "FAILURE",
                                      workflow="paquet", end=OLD, run=101)]
            self.assert_gate(rows, False, runs=runs)
            # Le run le plus récent est encore invisible dans le rollup.
            self.assert_gate(witness(), False, runs=runs)

    def test_active_run_without_any_packet_check_is_still_blocking(self):
        rows = [c for c in witness() if c["workflowName"] != "paquet"]
        self.assert_gate(rows, False, runs=[packet_run(status="in_progress", conclusion=None)])
        self.assert_gate(rows, False, runs=[packet_run(conclusion="failure")])
        self.assert_gate(rows, True, runs=[])
        other = {**packet_run(status="in_progress", conclusion=None),
                 "path": ".github/workflows/ui.yml"}
        self.assert_gate(rows, True, runs=[other])

    def test_latest_cancelled_run_must_have_its_own_cancelled_check(self):
        self.assert_gate(witness(), False, runs=[packet_run(), packet_run(101)])

    def test_unavailable_or_inconsistent_api_never_proves_replacement(self):
        self.assert_gate(witness(), False, api_error=True)
        self.assert_gate(witness(), False, runs=[])
        for changed in ({"head_sha": "b" * 40}, {"head_branch": "integration/other"},
                        {"event": "push"}, {"path": ".github/workflows/other.yml"},
                        {"conclusion": "failure"}, {"status": "in_progress"}):
            pr = {**packet_run(200, conclusion="success"), "event": "pull_request",
                  "path": ".github/workflows/tests.yml", **changed}
            self.assert_gate(witness(), False, pr_run=pr)

    def test_run_identity_requires_exact_repository_and_details_url(self):
        for index in (1, 4):
            for url in (None, "", "https://github.com/other/repo/actions/runs/100/job/123"):
                rows = witness()
                rows[index]["detailsUrl"] = url
                self.assert_gate(rows, False)

    def test_all_pages_are_read_before_choosing_latest_run(self):
        runs = [packet_run(i) for i in range(1, 101)]
        runs.append(packet_run(101, conclusion="failure"))
        self.assert_gate(witness(), False, runs=runs)

    def test_pure_predicate_does_not_discard_cancelled_checks_without_proof(self):
        self.assertFalse(radar.ci_green(witness()))

    def test_old_rerun_active_without_visible_checks_still_blocks(self):
        runs = [packet_run(99, status="in_progress", conclusion=None), packet_run()]
        self.assert_gate(witness(), False, runs=runs)

    def test_all_checks_finish_before_replacement_even_if_latest_rollup_hides_one(self):
        old = check("suite complète du paquet / tests", "FAILURE", workflow="paquet",
                    end="2026-10-20T12:30:00Z", run=99)
        old["startedAt"] = "2026-10-20T11:00:00Z"
        self.assert_gate(witness() + [old], False, runs=[packet_run(99), packet_run()])

    def test_duplicate_run_ids_never_prove_complete_pagination(self):
        self.assert_gate(witness(), False, runs=[packet_run(), packet_run()])

    def test_radar_reader_with_gh_object_uses_same_proof(self):
        for error in (False, True):
            fake = gh_for(witness(), api_error=error)

            class Gh:
                def run(self, argv, stdin=None):
                    proc = fake(argv)
                    return proc.returncode, proc.stdout, proc.stderr

            reader = radar.github_ci(types.SimpleNamespace(github="manaty/ameesh"), gh=Gh())
            self.assertEqual(reader("integration/paquet-h-2876")["green"], not error)


if __name__ == "__main__":
    unittest.main()
