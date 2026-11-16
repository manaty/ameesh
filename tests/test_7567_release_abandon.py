# SPDX-License-Identifier: AGPL-3.0-only
"""#7567: the pilot's coded abandon of a Nexlink cutoff stuck on red CI."""
import contextlib
import io
import json
import os
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from ameesh import nexlink_release as nx, release_cutoff as rc

SHA = "a" * 40
BASE = "b" * 40
RUN_ID = 43848262432
RUN_URL = "https://github.com/manaty/nexlink/actions/runs/%d" % RUN_ID


def run(conclusion="failure", *, status="completed", run_id=RUN_ID, url=RUN_URL):
    return {"databaseId": run_id, "status": status, "conclusion": conclusion,
            "url": url}


def pending(**change):
    row = dict(
        schema=rc.SCHEMA, project="nexlink", version="11bo", tag="release-11bo",
        integration="integration/release-11bo", head="c" * 40, base=BASE,
        main="d" * 40, sha=SHA, cutoff_at=1794823200.0, label="23:00",
        previous="e" * 40, empty=False, ci_max_inflight=3,
        ci_parts=[{"workflow": "sql-invariant.yml"},
                  {"workflow": "backend-suite.yml"}],
        dispatches={
            "ci/sql-invariant.yml": {"intent_at": 1.0, "accepted": True,
                                     "run": run()},
            "ci/backend-suite.yml": {"intent_at": 1.0, "accepted": True,
                                     "run": run("success", run_id=2,
                                                url="https://example/run/2")},
        })
    row.update(change)
    return row


class FailingPartsTests(unittest.TestCase):
    def test_only_a_completed_failure_proves_a_red_part(self):
        failures = nx.failing_ci_parts(pending())
        self.assertEqual(failures, [{"workflow": "sql-invariant.yml",
                                     "conclusion": "failure", "run": RUN_ID,
                                     "url": RUN_URL}])
        for status in ("queued", "in_progress"):
            self.assertEqual(nx.failing_ci_parts(pending(dispatches={
                "ci/sql-invariant.yml": {"run": run(status=status)}})), [])
        for conclusion in ("success", "cancelled", "timed_out", "skipped",
                           "neutral", "action_required", "stale"):
            self.assertEqual(nx.failing_ci_parts(pending(dispatches={
                "ci/sql-invariant.yml": {"run": run(conclusion)}})), [],
                             conclusion)
        self.assertEqual([row["conclusion"] for row in nx.failing_ci_parts(
            pending(dispatches={"ci/sql-invariant.yml": {
                "run": run("startup_failure", run_id=3)}}))],
            ["startup_failure"])
        # An intent with no observed run proves nothing either.
        self.assertEqual(nx.failing_ci_parts(pending(dispatches={
            "ci/sql-invariant.yml": {"intent_at": 1.0}})), [])

    def test_a_stale_dispatch_outside_the_plan_does_not_count(self):
        self.assertEqual(nx.failing_ci_parts(pending(
            ci_parts=[{"workflow": "backend-suite.yml"}])), [])

    def test_a_pending_without_a_ci_plan_has_no_conclusive_part(self):
        self.assertEqual(nx.failing_ci_parts(pending(ci_parts=[])), [])
        row = pending()
        row.pop("ci_parts")
        self.assertEqual(nx.failing_ci_parts(row), [])


class PlacedTagTests(unittest.TestCase):
    def test_own_version_or_a_tag_on_the_candidate_refuses(self):
        project = rc.ProjectRelease("nexlink", "/unused", model="nexlink")
        with mock.patch.object(nx, "release_tags", return_value={}):
            self.assertEqual(nx.placed_tag(project, pending()), "")
        with mock.patch.object(nx, "release_tags", return_value={"11bo": "f" * 40}):
            self.assertEqual(nx.placed_tag(project, pending()), "11bo")
        with mock.patch.object(nx, "release_tags", return_value={"11bn": SHA}):
            self.assertEqual(nx.placed_tag(project, pending()), "11bn")


class StagingInProgressTests(unittest.TestCase):
    def test_every_staging_step_is_named(self):
        self.assertIn("handoff", nx.staging_in_progress(pending(
            handoff={"to": "bureau"})))
        self.assertIn("attestation", nx.staging_in_progress(pending(
            staging_attestation={"actor": "bureau"})))
        self.assertIn("skip", nx.staging_in_progress(pending(
            handoff_skipped={"actor": "mesh-design"})))
        self.assertIn("migrations", nx.staging_in_progress(pending(
            migration_attestation={"hub": []})))
        self.assertIn("dispatch", nx.staging_in_progress(pending(dispatches={
            "staging/deploy-api.yml": {"intent_at": 1.0}})))
        self.assertEqual(nx.staging_in_progress(pending()), "")


class DropAbandonedTests(unittest.TestCase):
    def test_outcomes_name_what_happened_to_the_integration_ref(self):
        project = rc.ProjectRelease("nexlink", "/unused", model="nexlink")
        ref = "refs/heads/integration/release-11bo"
        for found, expected in (("", "absent"), (SHA, "dropped"),
                                ("f" * 40, "moved")):
            with self.subTest(found=found):
                slot = {"abandoned_integration": {
                    "ref": "integration/release-11bo", "sha": SHA}}
                saved = []
                calls = [types.SimpleNamespace(
                    stdout=found + "\t" + ref + "\n" if found else "")]
                if found == SHA:
                    calls.append(types.SimpleNamespace(stdout=""))
                with mock.patch.object(rc, "_git", side_effect=calls) as git:
                    outcome = nx.drop_abandoned(project, slot,
                                                lambda: saved.append(1))
                self.assertEqual(outcome, expected)
                self.assertNotIn("abandoned_integration", slot)
                self.assertEqual(saved, [1])
                # The delete push runs only while the ref still holds the
                # abandoned candidate; a moved ref is never force-pushed.
                self.assertEqual(git.call_count, 2 if found == SHA else 1)


class AbandonTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(temp.cleanup)
        self.cfg = types.SimpleNamespace(state_dir=temp.name, host="test-host")
        self.project = rc.ProjectRelease(
            "nexlink", "/unused", target="develop", github="acme/nexlink",
            model="nexlink", staging_handoff="bureau")
        self.db = object()
        self.write_state(pending(), last_result="released")
        self.reports, self.sent, self.drops = [], [], []
        patchers = [
            mock.patch.object(nx, "release_tags", return_value={}),
            mock.patch.object(nx, "drop_abandoned",
                              side_effect=lambda project, slot, save: self.drops.append(
                                  dict(slot.get("abandoned_integration") or {}))),
            mock.patch.object(rc, "write_report",
                              side_effect=lambda cfg, project, report:
                              self.reports.append(report) or "/state/report.json"),
            mock.patch.object(rc, "send_report",
                              side_effect=lambda *args, **kwargs:
                              self.sent.append((args, kwargs)) or
                              {"smichea": True, "mesh-design": True}),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_state(self, row, **slot):
        slot["pending_nexlink"] = row
        rc.save_state(self.cfg, {"schema": rc.SCHEMA, "projects": {
            "nexlink": slot, "ameesh": {"last_result": "released"}}})

    def slot(self):
        return rc.load_state(self.cfg)["projects"]["nexlink"]

    def test_abandons_a_conclusively_red_cutoff(self):
        result = nx.abandon(self.cfg, self.db, self.project, now=1794824000.0,
                            reason="  hotfix #7561   in develop ",
                            actor="mesh-design")
        slot = self.slot()
        self.assertNotIn("pending_nexlink", slot)
        self.assertEqual(slot["last_cutoff"], 1794823200.0)
        self.assertEqual(slot["last_result"], "refused-ci")
        trace = slot["abandoned_pending"]
        self.assertEqual((trace["version"], trace["sha"], trace["base"]),
                         ("11bo", SHA, BASE))
        self.assertEqual((trace["by"], trace["why"]),
                         ("mesh-design", "hotfix #7561 in develop"))
        self.assertIn("ci/sql-invariant.yml run %d" % RUN_ID, trace["red"])
        self.assertIn(RUN_URL, trace["red"])
        self.assertEqual(trace["failures"][0]["url"], RUN_URL)
        self.assertEqual(slot["abandoned_integration"],
                         {"ref": "integration/release-11bo", "sha": SHA})
        self.assertEqual(self.drops, [{"ref": "integration/release-11bo", "sha": SHA}])
        self.assertTrue(result["integration_dropped"])
        self.assertEqual(result["result"], "refused-ci")
        self.assertEqual(result["report_path"], "/state/report.json")

    def test_state_is_saved_before_the_ref_is_dropped_and_announced(self):
        seen = {}

        def drop(project, slot, save):
            seen["pending"] = "pending_nexlink" in slot
            seen["result"] = slot.get("last_result")
            seen["trace"] = slot.get("abandoned_pending")
        with mock.patch.object(nx, "drop_abandoned", side_effect=drop):
            nx.abandon(self.cfg, self.db, self.project, now=1794824000.0,
                       reason="red SQL invariant", actor="mesh-design")
        self.assertEqual((seen["pending"], seen["result"]),
                         (False, "refused-ci"))
        self.assertEqual(seen["trace"]["version"], "11bo")
        self.assertEqual(len(self.reports), 1)
        report = self.reports[0]
        self.assertEqual((report["result"], report["version"], report["actor"]),
                         ("refused-ci", "11bo", "mesh-design"))
        self.assertEqual(report["failures"][0]["run"], RUN_ID)
        self.assertEqual(report["abandoned_pending"]["why"], "red SQL invariant")
        args, kwargs = self.sent[0]
        body = args[3]
        self.assertIn("ABANDONNÉE", body)
        self.assertIn("ci/sql-invariant.yml run %d" % RUN_ID, body)
        self.assertIn("red SQL invariant", body)
        self.assertIn("ameesh release cutoff --project nexlink --force", body)
        self.assertIn("Aucun tag", body)
        self.assertTrue(kwargs["urgent"])

    def test_a_failed_ref_drop_keeps_the_trace_and_says_so(self):
        with mock.patch.object(nx, "drop_abandoned",
                               side_effect=rc.ReleaseError("git push failed")), \
                contextlib.redirect_stdout(io.StringIO()):
            result = nx.abandon(self.cfg, self.db, self.project, now=1794824000.0,
                                reason="red SQL invariant", actor="mesh-design")
        slot = self.slot()
        self.assertNotIn("pending_nexlink", slot)
        self.assertIn("abandoned_integration", slot)
        self.assertFalse(result["integration_dropped"])
        self.assertEqual(result["drop_error"], "git push failed")
        self.assertIn("retrait à reprendre : git push failed",
                      self.sent[0][0][3])

    def test_a_ref_moved_by_a_third_party_is_not_claimed_as_dropped(self):
        with mock.patch.object(nx, "drop_abandoned", return_value="moved"):
            result = nx.abandon(self.cfg, self.db, self.project, now=1794824000.0,
                                reason="red SQL invariant", actor="mesh-design")
        self.assertFalse(result["integration_dropped"])
        self.assertTrue(result["integration_moved"])
        self.assertIn("déplacée par un tiers", self.sent[0][0][3])

    def test_unrelated_project_is_untouched(self):
        nx.abandon(self.cfg, self.db, self.project, now=1794824000.0,
                   reason="red SQL invariant", actor="mesh-design")
        state = rc.load_state(self.cfg)
        self.assertEqual(state["projects"]["ameesh"], {"last_result": "released"})

    def test_an_older_pending_does_not_move_the_recorded_slot_backwards(self):
        self.write_state(pending(), last_cutoff=1794826800.0)
        nx.abandon(self.cfg, self.db, self.project, now=1794827000.0,
                   reason="red SQL invariant", actor="mesh-design")
        self.assertEqual(self.slot()["last_cutoff"], 1794826800.0)
        self.assertEqual(self.slot()["abandoned_pending"]["cutoff_at"],
                         1794823200.0)

    def assert_refused(self, pattern, *, pending_kept=True):
        with self.assertRaisesRegex(rc.ReleaseError, pattern):
            nx.abandon(self.cfg, self.db, self.project, now=1794824000.0,
                       reason="red SQL invariant", actor="mesh-design")
        if pending_kept:
            self.assertIn("pending_nexlink", self.slot())
        self.assertFalse(self.reports)
        self.assertFalse(self.sent)
        self.assertFalse(self.drops)

    def test_refuses_without_a_frozen_cutoff(self):
        rc.save_state(self.cfg, {"schema": rc.SCHEMA,
                                 "projects": {"nexlink": {}}})
        self.assert_refused("no frozen Nexlink cutoff", pending_kept=False)

    def test_refuses_without_a_conclusive_red_part(self):
        self.write_state(pending(dispatches={
            "ci/sql-invariant.yml": {"run": run(status="in_progress")},
            "ci/backend-suite.yml": {"run": run("cancelled", run_id=2)}}))
        self.assert_refused("no CI part is conclusively red")

    def test_refuses_a_published_tag(self):
        with mock.patch.object(nx, "release_tags", return_value={"11bo": SHA}):
            self.assert_refused("release-11bo is already tagged")

    def test_refuses_an_open_staging_handoff(self):
        self.write_state(pending(handoff={"to": "bureau"}))
        self.assert_refused("staging handoff is open")

    def test_refuses_a_started_staging_dispatch(self):
        self.write_state(pending(dispatches={
            "ci/sql-invariant.yml": {"run": run()},
            "staging/deploy-api.yml": {"intent_at": 2.0}}))
        self.assert_refused("staging dispatch was already started")

    def test_refuses_a_reasonless_request(self):
        with self.assertRaisesRegex(rc.ReleaseError, "requires --reason"):
            nx.abandon(self.cfg, self.db, self.project, now=1.0, reason="  ",
                       actor="mesh-design")
        self.assertIn("pending_nexlink", self.slot())
        self.assertFalse(self.reports)
        self.assertFalse(self.sent)

    def test_refuses_an_incomplete_pending(self):
        self.write_state(pending(sha=None))
        self.assert_refused("pending_nexlink is incomplete")

    def test_refuses_a_semver_project(self):
        project = rc.ProjectRelease("ameesh", "/unused", model="semver")
        with self.assertRaisesRegex(rc.ReleaseError, "nexlink model only"):
            nx.abandon(self.cfg, self.db, project, now=1.0, reason="why",
                       actor="mesh-design")

    def test_a_second_abandon_refuses(self):
        nx.abandon(self.cfg, self.db, self.project, now=1794824000.0,
                   reason="red SQL invariant", actor="mesh-design")
        with self.assertRaisesRegex(rc.ReleaseError, "no frozen Nexlink cutoff"):
            nx.abandon(self.cfg, self.db, self.project, now=1794824001.0,
                       reason="again", actor="mesh-design")

    def test_refuses_while_the_release_lock_is_held(self):
        lock = os.path.join(self.cfg.state_dir, "release", "release.lock")
        with rc._locked(lock):
            with self.assertRaisesRegex(rc.ReleaseBusy, "another release holds"):
                nx.abandon(self.cfg, self.db, self.project, now=1794824000.0,
                           reason="red SQL invariant", actor="mesh-design")
        self.assertIn("pending_nexlink", self.slot())
        self.assertFalse(self.reports)
        self.assertFalse(self.sent)


class AbandonWithRealRefTests(unittest.TestCase):
    """One real bare remote: no tag, the integration ref is dropped by lease."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.remote, self.repo = root / "remote.git", root / "repo"
        subprocess.check_call(["git", "init", "-q", "--bare", str(self.remote)])
        subprocess.check_call(["git", "init", "-q", str(self.repo)])
        for key, value in (("user.name", "t"), ("user.email", "t@localhost")):
            self.git("config", key, value)
        self.git("remote", "add", "origin", str(self.remote))
        (self.repo / "f").write_text("candidate\n")
        self.git("add", ".")
        self.git("commit", "-qm", "candidate")
        self.sha = self.git("rev-parse", "HEAD").strip()
        self.ref = "refs/heads/integration/release-11bo"
        self.git("push", "-q", "origin", self.sha + ":" + self.ref)
        self.cfg = types.SimpleNamespace(state_dir=str(root / "state"), host="test")
        self.db = object()
        self.project = rc.ProjectRelease("nexlink", str(self.repo), target="develop",
                                         model="nexlink")
        rc.save_state(self.cfg, {"schema": rc.SCHEMA, "projects": {"nexlink": {
            "pending_nexlink": pending(sha=self.sha)}}})

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], text=True)

    def test_the_ref_is_dropped_and_a_durable_report_written(self):
        with mock.patch.object(rc, "send_report",
                               return_value={"smichea": True}) as sent:
            result = nx.abandon(self.cfg, self.db, self.project, now=1794824000.0,
                                reason="hotfix #7561 in develop",
                                actor="mesh-design")
        self.assertEqual(self.git("ls-remote", "origin", self.ref), "")
        slot = rc.load_state(self.cfg)["projects"]["nexlink"]
        self.assertNotIn("pending_nexlink", slot)
        self.assertNotIn("abandoned_integration", slot)
        self.assertEqual(slot["abandoned_pending"]["sha"], self.sha)
        path = result["report_path"]
        self.assertTrue(os.path.exists(path))
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        with open(path, encoding="utf-8") as stream:
            written = json.load(stream)
        self.assertEqual((written["result"], written["version"], written["actor"]),
                         ("refused-ci", "11bo", "mesh-design"))
        self.assertEqual(written["release_sha"], self.sha)
        self.assertTrue(sent.call_args.kwargs["urgent"])
        self.assertIn(RUN_URL, sent.call_args.args[3])


class AbandonCommandTests(unittest.TestCase):
    def args(self, **change):
        values = dict(project="nexlink", reason="red SQL invariant", actor=None,
                      now=None, json=False)
        values.update(change)
        return types.SimpleNamespace(**values)

    def run_abandon(self, args, *, cfg=None, corrector=None, projects=None,
                    report=None, failure=None, clock=1794824000.0,
                    election=None, election_error=None):
        from ameesh import release, role_run, work
        project = rc.ProjectRelease("nexlink", "/unused", target="develop",
                                    model="nexlink")
        declared = projects if projects is not None else {"nexlink": project}
        corrector = corrector or (lambda *a, **k: "mesh-design")
        cfg = cfg if cfg is not None else types.SimpleNamespace(host="test-host")
        election = election or {"held": True, "host": "test-host"}
        engine = ({"side_effect": failure} if failure is not None else
                  {"return_value": report or dict(
                      result="refused-ci", version="11bo",
                      integration="integration/release-11bo",
                      integration_dropped=True, drop_error="",
                      failures=[{"workflow": "sql-invariant.yml",
                                 "run": RUN_ID}])})
        live = ({"side_effect": election_error} if election_error is not None
                else {"return_value": election})
        with mock.patch.object(rc, "projects", return_value=declared), \
                mock.patch("ameesh.db.open_db") as opened, \
                mock.patch.object(work, "corrector", side_effect=corrector) as who, \
                mock.patch.object(role_run, "live", **live) as elected, \
                mock.patch.object(release.time, "time", return_value=clock), \
                mock.patch.object(nx, "abandon", **engine) as abandon, \
                mock.patch("builtins.print"):
            status = release._abandon(cfg, args)
        return status, who, abandon, opened, elected

    def test_the_command_verifies_the_pilot_and_passes_the_reason(self):
        status, who, abandon, opened, _ = self.run_abandon(self.args())
        self.assertEqual(status, 0)
        self.assertEqual(who.call_args.kwargs["what"], "release abandon")
        self.assertFalse(who.call_args.kwargs["history"])
        self.assertEqual(abandon.call_args.kwargs,
                         {"now": 1794824000.0, "reason": "red SQL invariant",
                          "actor": "mesh-design"})
        opened.return_value.close.assert_called_once_with()

    def test_the_trace_keeps_the_real_hour_within_the_now_window(self):
        status, _, abandon, _, _ = self.run_abandon(
            self.args(now=1794824000.0 + 120.0))
        self.assertEqual(status, 0)
        self.assertEqual(abandon.call_args.kwargs["now"], 1794824000.0)

    def test_a_now_outside_the_clock_window_is_refused(self):
        for offset in (-3600.0, 3600.0):
            with self.subTest(offset=offset):
                status, who, abandon, _, _ = self.run_abandon(
                    self.args(now=1794824000.0 + offset))
                self.assertEqual(status, 1)
                abandon.assert_not_called()
                who.assert_not_called()

    def test_a_refused_pilot_does_not_touch_the_state(self):
        from ameesh import work
        status, who, abandon, _, _ = self.run_abandon(
            self.args(), corrector=lambda *a, **k:
            (_ for _ in ()).throw(work.WorkError("not a pilot")))
        self.assertEqual(status, 1)
        abandon.assert_not_called()

    def test_an_engine_refusal_is_reported_and_the_db_closed(self):
        status, who, abandon, opened, _ = self.run_abandon(
            self.args(), failure=rc.ReleaseError(
                "Nexlink 11bo cannot be abandoned: no CI part is conclusively red"))
        self.assertEqual(status, 1)
        abandon.assert_called_once()
        opened.return_value.close.assert_called_once_with()

    def test_an_undeclared_or_semver_project_is_refused(self):
        status, who, abandon, _, _ = self.run_abandon(self.args(), projects={})
        self.assertEqual(status, 1)
        abandon.assert_not_called()
        semver = {"nexlink": rc.ProjectRelease("nexlink", "/unused", model="semver")}
        status, who, abandon, _, _ = self.run_abandon(self.args(), projects=semver)
        self.assertEqual(status, 1)
        abandon.assert_not_called()

    def test_the_elected_release_host_may_abandon(self):
        cfg = types.SimpleNamespace(host="elected",
                                    release={"role": "mesh-design"})
        status, who, abandon, _, elected = self.run_abandon(
            self.args(), cfg=cfg, election={"held": True, "host": "elected"})
        self.assertEqual(status, 0)
        elected.assert_called_once()
        self.assertEqual(elected.call_args.args[1], "mesh-design")
        abandon.assert_called_once()

    def test_another_host_is_refused_before_the_pilot_is_even_named(self):
        cfg = types.SimpleNamespace(host="other",
                                    release={"role": "mesh-design"})
        status, who, abandon, _, _ = self.run_abandon(
            self.args(), cfg=cfg, election={"held": True, "host": "elected"})
        self.assertEqual(status, 1)
        who.assert_not_called()
        abandon.assert_not_called()

    def test_a_release_role_without_a_live_lease_is_refused(self):
        cfg = types.SimpleNamespace(host="elected",
                                    release={"role": "mesh-design"})
        status, who, abandon, _, _ = self.run_abandon(
            self.args(), cfg=cfg, election={"held": False, "host": "elected"})
        self.assertEqual(status, 1)
        who.assert_not_called()
        abandon.assert_not_called()

    def test_a_host_without_a_configured_role_keeps_the_local_gesture(self):
        status, who, abandon, _, elected = self.run_abandon(
            self.args(), cfg=types.SimpleNamespace(host="test-host"))
        self.assertEqual(status, 0)
        elected.assert_not_called()
        abandon.assert_called_once()

    def test_an_unreadable_election_is_refused(self):
        from ameesh import role_run
        cfg = types.SimpleNamespace(host="elected",
                                    release={"role": "mesh-design"})
        status, who, abandon, _, _ = self.run_abandon(
            self.args(), cfg=cfg,
            election_error=role_run.RoleError("unknown role (no agent_registry row)"))
        self.assertEqual(status, 1)
        who.assert_not_called()
        abandon.assert_not_called()

    def test_release_and_packet_watch_roles_must_agree(self):
        cfg = types.SimpleNamespace(host="elected",
                                    release={"role": "mesh-design"},
                                    packet_watch={"role": "bureau"})
        status, who, abandon, _, elected = self.run_abandon(self.args(), cfg=cfg)
        self.assertEqual(status, 1)
        elected.assert_not_called()
        abandon.assert_not_called()


if __name__ == "__main__":
    unittest.main()
