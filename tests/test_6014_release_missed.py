# SPDX-License-Identifier: AGPL-3.0-only
"""#6014: a due release cutoff that was not done must say so.

The Nexlink model recorded a missed slot in its local state and returned
without a report; the thread entry pointed at the sender's project (ameesh)
even for the semver model. The paused train also had no alert at all.
"""
from __future__ import annotations

import json
import os
import tempfile
import types
import unittest
from unittest import mock

from ameesh import alert_briefs, exploitation, nexlink_release as nx
from ameesh import release, release_cutoff as rc


class SendReportThreadTest(unittest.TestCase):
    def setUp(self):
        self.cfg = types.SimpleNamespace(state_dir=tempfile.mkdtemp(prefix="6014-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.cfg.state_dir, ignore_errors=True))
        self.project = rc.ProjectRelease("nexlink", "/unused", target="develop",
                                         model="nexlink")

    def test_report_is_recorded_in_the_release_project_thread(self):
        with mock.patch.object(rc, "report_recipients", return_value=["orchestrateur"]), \
                mock.patch.object(rc.mail, "send", return_value=42) as send, \
                mock.patch("ameesh.fil.record") as record:
            rc.send_report(self.cfg, object(), self.project, "corps", {"schema": "x"},
                           urgent=True)
        send.assert_called_once()
        self.assertEqual(send.call_args.kwargs["thread"], False)
        self.assertTrue(send.call_args.kwargs["payload"]["urgent"])
        record.assert_called_once()
        kwargs = record.call_args.kwargs
        self.assertEqual(kwargs["project"], "nexlink")
        self.assertEqual(kwargs["mailbox_ids"], [42])
        self.assertEqual(kwargs["text"], "corps")

    def test_a_mail_failure_does_not_try_to_record_an_absent_entry(self):
        with mock.patch.object(rc, "report_recipients", return_value=["orchestrateur"]), \
                mock.patch.object(rc.mail, "send", side_effect=RuntimeError("db down")), \
                mock.patch("ameesh.fil.record") as record:
            delivered = rc.send_report(self.cfg, object(), self.project, "corps", {})
        record.assert_not_called()
        self.assertEqual(delivered, {"orchestrateur": False})


class NexlinkMissedTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.temp.cleanup)
        self.cfg = types.SimpleNamespace(state_dir=self.temp.name, host="test-host")
        self.project = rc.ProjectRelease("nexlink", "/unused", target="develop",
                                         github="acme/nexlink", model="nexlink")
        state = {"schema": rc.SCHEMA, "projects": {"nexlink": {
            "last_error": "git ls-remote failed", "last_error_at": 1100.0}}}
        rc.save_state(self.cfg, state)
        for name, value in (("compiled_rules", (object(), types.SimpleNamespace(commit="d" * 40))),
                            ("cutoff_slot", (1000.0, "23:00")), ("fetch", None)):
            patcher = mock.patch.object(rc, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_missed_cutoff_announces_and_names_the_cause(self):
        with mock.patch.object(rc, "send_report") as send:
            report = nx.execute(self.cfg, object(), self.project, now=4600.0)
        self.assertEqual(report["result"], "missed")
        self.assertEqual(report["cutoff_at"], 1000.0)
        self.assertIn("did not start", report["message"])
        self.assertEqual(report["attempt_error"], "git ls-remote failed")
        self.assertEqual(report["host"], "test-host")
        send.assert_called_once()
        self.assertTrue(send.call_args.kwargs["urgent"])
        self.assertIn("MANQUÉE", send.call_args.args[3])
        slot = rc.load_state(self.cfg)["projects"]["nexlink"]
        self.assertEqual(slot["last_cutoff"], 1000.0)
        self.assertEqual(slot["last_result"], "missed")

    def test_a_train_paused_cause_is_reported_when_available(self):
        fake_db = types.SimpleNamespace(query=lambda *a, **k: [])
        with mock.patch.object(rc, "send_report"), \
                mock.patch.object(nx, "_train_pause",
                                  return_value="train nexlink:develop paused since 2026-10-26T06:00Z"):
            report = nx.execute(self.cfg, fake_db, self.project, now=4600.0)
        self.assertIn("train", report)
        self.assertIn("paused", report["train"])


class NoStagingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.temp.cleanup)
        self.cfg = types.SimpleNamespace(state_dir=self.temp.name)
        self.project = rc.ProjectRelease("nexlink", "/unused", target="develop",
                                         model="nexlink")
        pending = dict(sha="a" * 40, main="b" * 40, base="c" * 40,
                       tag="nexlink-11w", version="11w", cutoff_at=100.0,
                       label="11:00", integration="integration/release-11w",
                       empty=False, ci_max_inflight=3,
                       ci_parts=[{"workflow": "backend-suite.yml"}])
        rc.save_state(self.cfg, {"schema": rc.SCHEMA,
                                 "projects": {"nexlink": {"pending_nexlink": pending}}})
        for name, value in (("compiled_rules", (object(), types.SimpleNamespace(commit="d" * 40))),
                            ("cutoff_slot", (1000.0, "23:00")), ("fetch", None)):
            patcher = mock.patch.object(rc, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(nx, "ensure_ref")
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(nx, "release_tags", return_value={})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_no_staging_does_not_refuse_the_nexlink_model(self):
        with mock.patch.object(nx, "workflow_step", return_value=False):
            report = nx.execute(self.cfg, None, self.project, now=100000.0,
                                no_staging=True)
        self.assertEqual(report["result"], "waiting-ci")

    def test_no_staging_still_runs_every_nexlink_staging_workflow(self):
        with mock.patch.object(nx, "workflow_step", return_value=True) as step, \
                mock.patch.object(nx, "publish", return_value="https://example/pr/1"), \
                mock.patch.object(nx, "migration_gate", return_value={}):
            report = nx.execute(self.cfg, None, self.project, now=100000.0,
                                no_staging=True)
        self.assertEqual(report["result"], "released")
        staging = [call for call in step.call_args_list if call.kwargs["staging"]]
        self.assertEqual(len(staging), 3)
        self.assertEqual({call.args[2] for call in staging},
                         set(nx.STAGING_WORKFLOWS))


class UndeclaredReleaseTest(unittest.TestCase):
    AT = 1_800_000_000.0

    def setUp(self):
        self.cfg = types.SimpleNamespace(
            host="release-candidate",
            release={"role": "mesh-design", "projects": {}},
            github={"projects": {"ameesh": "manaty/ameesh",
                                 "nexlink": {"repo": "manaty/nexlink"}}})

    def test_a_due_cutoff_for_an_undeclared_project_is_alerted(self):
        with mock.patch.object(rc, "cutoff_slot", return_value=(self.AT, "11:00")):
            alerts = exploitation._release_undeclared(
                self.cfg, None, self.AT + rc.WINDOW_S + 1,
                projects={"ameesh", "nexlink"}, declared={"ameesh"},
                candidate=True, rules_of=lambda name: object())
        self.assertEqual(len(alerts), 1)
        alert = alerts[0]
        self.assertEqual(alert["type"], "release_project_undeclared")
        self.assertEqual(alert["project"], "nexlink")
        self.assertIn("release.projects.nexlink", alert["detail"])
        self.assertIn("mesh-design", alert["detail"])

    def test_a_declared_project_or_an_out_of_window_cutoff_is_silent(self):
        with mock.patch.object(rc, "cutoff_slot", return_value=(self.AT, "11:00")):
            self.assertEqual(exploitation._release_undeclared(
                self.cfg, None, self.AT + rc.WINDOW_S + 1,
                projects={"nexlink"}, declared={"nexlink"},
                candidate=True, rules_of=lambda name: object()), [])
            self.assertEqual(exploitation._release_undeclared(
                self.cfg, None, self.AT + 1,
                projects={"nexlink"}, declared={"ameesh"},
                candidate=True, rules_of=lambda name: object()), [])

    def test_a_host_outside_the_release_role_is_silent(self):
        with mock.patch.object(rc, "cutoff_slot", return_value=(self.AT, "11:00")):
            self.assertEqual(exploitation._release_undeclared(
                self.cfg, None, self.AT + rc.WINDOW_S + 1,
                projects={"nexlink"}, declared={"ameesh"},
                candidate=False, rules_of=lambda name: object()), [])

    def test_the_alert_is_registered_and_briefed(self):
        self.assertIn("release_project_undeclared", exploitation.ALERT_TYPES)
        self.assertIn("release_project_undeclared", alert_briefs.BRIEFED_TYPES)


class ConfigurationErrorTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.temp.cleanup)
        self.cfg = types.SimpleNamespace(state_dir=self.temp.name, host="test-host",
                                         humans="", release={"projects": {}})

    def test_an_explicit_project_must_be_declared_even_with_no_projects(self):
        with self.assertRaises(rc.ReleaseError):
            release._project_names(self.cfg, types.SimpleNamespace(project="nexlink"))
        self.cfg.release = {"projects": {"nexlink": {"repo": "/unused", "target": "develop",
                                                     "github": "acme/nexlink",
                                                     "model": "nexlink"}}}
        self.assertEqual(
            release._project_names(self.cfg, types.SimpleNamespace(project="nexlink")),
            ["nexlink"])

    def test_configuration_error_is_reported_then_throttled(self):
        fake_db = types.SimpleNamespace(close=lambda: None)
        with mock.patch("ameesh.db.open_db", return_value=fake_db), \
                mock.patch.object(rc, "send_report") as send:
            release._report_configuration_error(self.cfg, "nexlink",
                                                RuntimeError("not declared"), now=1_800_000_000.0)
            release._report_configuration_error(self.cfg, "nexlink",
                                                RuntimeError("not declared"), now=1_800_000_001.0)
            release._report_configuration_error(self.cfg, "nexlink",
                                                RuntimeError("not declared"),
                                                now=1_800_000_000.0 + 6 * 3600 + 1)
        self.assertEqual(send.call_count, 2)
        self.assertIn("not declared", send.call_args.args[3])

    def test_tick_reports_an_undeclared_explicit_project(self):
        args = types.SimpleNamespace(project="nexlink", now=None, dry_run=False, json=False)
        with mock.patch.object(release, "_report_configuration_error") as report:
            self.assertEqual(release._release_tick(self.cfg, args, {}), 1)
        report.assert_called_once()
        self.assertEqual(report.call_args.args[1], "nexlink")


class TrainPausedAlertTest(unittest.TestCase):
    class Db:
        def __init__(self, rows):
            self.rows = rows

        def query(self, *args, **kwargs):
            return self.rows

    def test_paused_for_less_than_a_day_is_not_alerted(self):
        db = self.Db([{"target_key": "nexlink:develop", "revision": 133,
                       "document": {"phase": "paused", "started_ts": 1000.0,
                                    "failures": 1}}])
        self.assertEqual(exploitation._train_paused(db, 1000.0 + 23 * 3600), [])

    def test_paused_for_more_than_a_day_raises_one_alert(self):
        db = self.Db([{"target_key": "nexlink:develop", "revision": 133,
                       "document": {"phase": "paused", "started_ts": 1000.0,
                                    "failures": 1}}])
        alerts = exploitation._train_paused(db, 1000.0 + 25 * 3600)
        self.assertEqual(len(alerts), 1)
        alert = alerts[0]
        self.assertEqual(alert["type"], "train_paused")
        self.assertEqual(alert["project"], "nexlink")
        self.assertIn("still paused", alert["detail"])
        self.assertIn("train_paused", alert_briefs.BRIEFED_TYPES)
        self.assertIn("release_project_undeclared", alert_briefs.BRIEFED_TYPES)

    def test_a_non_paused_train_is_ignored(self):
        db = self.Db([{"target_key": "nexlink:develop", "revision": 134,
                       "document": {"phase": "empty", "started_ts": 1000.0}}])
        self.assertEqual(exploitation._train_paused(db, 1000.0 + 48 * 3600), [])

    def test_unreadable_state_is_not_a_pause(self):
        db = types.SimpleNamespace(query=mock.Mock(side_effect=RuntimeError("db down")))
        self.assertEqual(exploitation._train_paused(db, 1000.0), [])


class TransientReportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.temp.cleanup)
        self.cfg = types.SimpleNamespace(state_dir=self.temp.name)
        self.project = rc.ProjectRelease("nexlink", "/unused", target="develop",
                                         model="nexlink")
        for name, value in (("compiled_rules", (object(), types.SimpleNamespace(commit="d" * 40))),
                            ("cutoff_slot", (1000.0, "23:00"))):
            patcher = mock.patch.object(rc, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_transient_failure_is_reported_once_per_cutoff(self):
        exc = rc.TransientReleaseError("git fetch failed")
        with mock.patch.object(rc, "send_report") as send:
            release._report_error(self.cfg, object(), self.project, exc, now=4600.0)
            release._report_error(self.cfg, object(), self.project, exc, now=4700.0)
        send.assert_called_once()
        report = send.call_args.args[4]
        self.assertTrue(report["transient"])
        self.assertIn("transitoire", send.call_args.args[3])
        slot = rc.load_state(self.cfg)["projects"]["nexlink"]
        self.assertEqual(slot["last_error"], "git fetch failed")


if __name__ == "__main__":
    unittest.main()
