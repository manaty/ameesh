# SPDX-License-Identifier: AGPL-3.0-only
"""#7605: packet 657 had five runs and an announced, undispatched sixth part."""
import shlex
import types
import unittest
from datetime import datetime
from unittest import mock

from ameesh import ci, merge_queue, plan_github, work

REPO = "manaty/nexlink"
SHA = "22de8527c8d0f925f39f14d5a3268f6f1c9bc38b"
BRANCH = "integration/paquet-657-nexlink"
PARTS = ("backend-suite.yml", "sql-invariant.yml", "scripts-suite.yml",
         "sync-checks.yml", "rollback-checks.yml", "civil-crypto.yml")
# GitHub commit-status history of 22de8527, read 2026-11-16. The target URL
# points at backend-suite's run, not a civil-crypto run: it cannot prove dispatch.
ANNOUNCEMENT = dict(context="lot/civil-crypto", state="pending",
                    description="part due du change set, en attente de son run de dispatch",
                    created_at="2026-11-16T18:43:49Z",
                    target_url="https://github.com/manaty/nexlink/actions/runs/43931473681")
RUN_IDS = (43931473681, 43931477822, 43931484441, 43931490481, 43931496836)


class UndispatchedPartsTest(unittest.TestCase):
    def setUp(self):
        self.cfg = types.SimpleNamespace(github={"projects": {"nexlink": {
            "repo": REPO, "workflows": list(PARTS)}}})
        self.lot = dict(id=7599, app="nexlink", issue_ref=REPO + "#4718",
                        branch=BRANCH, state="qa", assignee="composer")
        self.runs = {w: [dict(id=i, path=".github/workflows/" + w, head_sha=SHA,
                             head_branch=BRANCH, status="completed", conclusion="success")]
                     for w, i in zip(PARTS, RUN_IDS)}
        self.runs[PARTS[-1]] = []
        self.paths = []
        self.errors = {}
        self.heads = [SHA]
        self.logs = []
        self.keys = set()
        self.st = types.SimpleNamespace(mailbox=mock.Mock())
        self.st.mailbox.by_idempotency_key.side_effect = lambda sender, to, key: key in self.keys
        self.observed = datetime.fromisoformat("2026-11-16T18:44:00+00:00").timestamp()
        self.watcher = ci.Watcher(self.cfg, api=self.api, log=self.logs.append,
                                  clock=lambda: self.observed)
        self.plan = {w: {"expected_sha": SHA} for w in PARTS}
        self.plan["sql-invariant.yml"].update(base_sha="a" * 40, head_sha=SHA)
        self.enter(mock.patch.object(ci.storage, "of", return_value=self.st))
        self.enter(mock.patch.object(work, "milestones", return_value=[dict(kind="frozen", sha=SHA)]))
        self.gate = self.enter(mock.patch.object(merge_queue, "review_gate_for_item",
                                                return_value={"ok": True}))
        self.planner = self.enter(mock.patch.object(ci, "_dispatch_plan", return_value=self.plan))
        self.pilot = self.enter(mock.patch.object(self.watcher, "_merge_pilot", return_value="pilot"))
        self.send = self.enter(mock.patch.object(ci, "_send", side_effect=self.sent))

    def enter(self, context):
        value = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        return value

    def sent(self, *args, **kwargs):
        self.keys.add(kwargs["idempotency_key"])
        return 42

    def api(self, path):
        self.paths.append(path)
        if "/branches/" in path:
            return {"commit": {"sha": self.heads.pop(0) if len(self.heads) > 1 else self.heads[0]}}
        if "/runs?" in path:
            workflow = path.split("/workflows/")[1].split("/")[0]
            if workflow in self.errors:
                error = self.errors[workflow]
                if isinstance(error, Exception):
                    raise error
                return error
            return {"total_count": len(self.runs[workflow]), "workflow_runs": self.runs[workflow]}
        raise AssertionError(path)

    def observe(self):
        return self.watcher._undispatched_parts(None, REPO, [self.lot])

    def test_packet_657_alerts_before_slo_with_only_the_missing_part_command(self):
        self.assertEqual(ANNOUNCEMENT["created_at"], "2026-11-16T18:43:49Z")
        self.assertLess(self.observed, datetime.fromisoformat("2026-11-16T19:30:00+00:00").timestamp())
        records = self.observe()
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual((record["workflow"], record["to"], record["mode"]),
                         ("civil-crypto.yml", "pilot", "wake"))
        self.assertEqual(shlex.split(record["command"]), [
            "gh", "workflow", "run", "civil-crypto.yml", "--repo", REPO,
            "--ref", BRANCH, "-f", "expected_sha=" + SHA])
        self.assertIn("has no run", ci.describe(record))
        self.assertFalse(self.send.call_args.kwargs["payload"].get("info"))
        self.assertEqual(self.send.call_args.kwargs["lot"], 7599)
        # Neither the announcement's URL nor SUITE_RE selects the due parts.
        self.assertFalse(ci.SUITE_RE.search("civil-crypto.yml"))
        self.assertTrue(all("head_sha=" + SHA in p and "branch=" + ci._q(BRANCH) in p
                            for p in self.paths if "/runs?" in p))

    def test_queue_running_and_terminal_runs_are_all_already_dispatched(self):
        for status in ("queued", "pending", "in_progress", "completed"):
            with self.subTest(status=status):
                self.runs[PARTS[-1]] = [dict(id=43940275049,
                    path=".github/workflows/civil-crypto.yml", head_sha=SHA,
                    head_branch=BRANCH, status=status, conclusion="cancelled")]
                self.assertEqual(self.observe(), [])
        self.send.assert_not_called()

    def test_restart_does_not_repeat_a_delivered_alert_and_observation_is_fresh(self):
        self.observe()
        self.assertEqual(self.observe(), [])
        self.watcher._dispatch_plans.clear()  # a service restart retains the mailbox proof
        self.assertEqual(self.observe(), [])
        self.send.assert_called_once()
        self.assertEqual(sum("/runs?" in p for p in self.paths), 18)

    def test_additional_inputs_and_shell_quoting_are_preserved(self):
        self.runs["sql-invariant.yml"] = []
        self.plan["civil-crypto.yml"]["reason"] = "a 'quote'; $(false)"
        records = self.observe()
        sql, civil = records
        self.assertIn("base_sha=" + "a" * 40, shlex.split(sql["command"]))
        self.assertIn("head_sha=" + SHA, shlex.split(sql["command"]))
        self.assertIn("reason=a 'quote'; $(false)", shlex.split(civil["command"]))

    def test_branch_moved_before_or_during_read_never_alerts(self):
        for heads in (["a" * 40], [SHA, "a" * 40]):
            with self.subTest(heads=heads):
                self.heads = list(heads)
                self.assertEqual(self.observe(), [])
        self.send.assert_not_called()

    def test_unreadable_or_wrong_commit_is_not_proof_of_absence(self):
        for response in ({}, {"workflow_runs": None}, {"total_count": 1, "workflow_runs": []},
                {"total_count": 1, "workflow_runs": [dict(
                head_sha="a" * 40, head_branch=BRANCH, path="civil-crypto.yml")]},
                plan_github.GithubError("unavailable")):
            with self.subTest(response=response):
                self.errors[PARTS[-1]] = response
                self.assertEqual(self.observe(), [])
        self.send.assert_not_called()
        self.assertEqual(len(self.logs), 5)

    def test_required_reviews_precede_repository_planner_execution(self):
        self.gate.return_value = {"ok": False}
        self.assertEqual(self.observe(), [])
        self.planner.assert_not_called()

    def test_label_driven_repository_keeps_its_existing_supervision(self):
        settings = mock.Mock()
        settings.workflows_for.return_value = (ci.WORKFLOWS[0],)
        with mock.patch.object(plan_github, "settings_of", return_value=settings), \
                mock.patch.object(ci, "_carries_freeze_request", return_value=True):
            self.assertEqual(self.observe(), [])
        self.planner.assert_not_called()

    def test_unreadable_planner_retries_instead_of_caching_an_empty_plan(self):
        self.planner.side_effect = ci.GelError("selector unavailable")
        self.assertEqual(self.observe(), [])
        self.assertIn("selector unavailable", self.logs[0])
        self.send.assert_not_called()
        self.planner.side_effect = None
        self.assertEqual(len(self.observe()), 1)

    def test_mail_failure_is_retryable(self):
        self.send.side_effect = RuntimeError("mail unavailable")
        self.assertEqual(self.observe(), [])
        self.assertFalse(self.keys)
        self.send.side_effect = self.sent
        self.assertEqual(len(self.observe()), 1)

    def test_watcher_calls_completeness_even_when_there_are_no_live_runs(self):
        # The original hole: neither live-run tracking nor catch-up sees a
        # workflow with no run. Exercise the real watch entry, not just helper.
        self.st.work = mock.Mock()
        self.st.work.open_for_sweep.return_value = [self.lot]
        self.st.ci = mock.Mock()
        self.st.ci.running.return_value = []
        with mock.patch.object(self.watcher, "followed_workflows", return_value=PARTS), \
                mock.patch.object(self.watcher, "bases", return_value=()), \
                mock.patch.object(self.watcher, "_listed", return_value=[]), \
                mock.patch.object(self.watcher, "_unblock", return_value=[]), \
                mock.patch.object(self.watcher, "_catch_up", return_value=[]), \
                mock.patch.object(self.watcher, "_refresh_bases"), \
                mock.patch.object(self.watcher, "_guard"), \
                mock.patch.object(self.watcher, "_map"), \
                mock.patch.object(self.watcher, "_disks"):
            records = self.watcher.watch(None, REPO)
        self.assertEqual([r["workflow"] for r in records], ["civil-crypto.yml"])

    def test_other_repository_and_unit_lots_are_ignored(self):
        self.lot["issue_ref"] = "other/repo#42"
        self.assertEqual(self.observe(), [])
        self.lot.update(issue_ref=REPO + "#4718", branch="lot/7589-fix")
        self.assertEqual(self.observe(), [])
        self.planner.assert_not_called()

    def test_no_pilot_is_logged_without_claiming_delivery(self):
        self.pilot.return_value = None
        self.assertEqual(self.observe(), [])
        self.assertIn("no pilot readable", self.logs[0])
        self.send.assert_not_called()

    def test_new_head_gets_a_new_alert(self):
        self.observe()
        next_sha = "b" * 40
        with mock.patch.object(work, "milestones", return_value=[dict(kind="frozen", sha=next_sha)]):
            self.heads = [next_sha]
            for rows in self.runs.values():
                for row in rows:
                    row["head_sha"] = next_sha
            for inputs in self.plan.values():
                inputs["expected_sha"] = next_sha
            records = self.observe()
        self.assertEqual(len(records), 1)
        self.assertIn(next_sha, records[0]["command"])
        self.assertEqual(self.send.call_count, 2)


if __name__ == "__main__":
    unittest.main()
