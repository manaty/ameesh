# SPDX-License-Identifier: AGPL-3.0-only
"""#6520: a release tick reads the cutoff's tag and the lock holder first.

The false alarm of 09/11 said "Aucun tag" while v1.46.0 was tagged and its
staging held the operator lock. These tests keep the tag-aware report, the
elected staging handoff (queued, not busy), and the named holder.
"""
from __future__ import annotations

import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

from ameesh import config, release, release_cutoff, staging_watch
from tests.test_2448_release_cutoff import ReleaseFixtureMixin, _paris

SHA = "a" * 40
OTHER = "b" * 40


class FakeRules:
    def __init__(self, at=100.0):
        self.at = at

    def zone(self):
        return ZoneInfo("UTC")

    def cutoff(self, now):
        return self.at

    @property
    def cutoffs(self):
        return ["11:00"]


def tag(version="1.46.0", commit=SHA):
    return {"version": version, "tag": "v" + version, "commit": commit, "at": 100.0}


def holders(request, lease):
    """Run staging_holders against a fake shared DB (one elected key)."""
    class Work:
        def land_holder(self, key):
            return lease

    class Events:
        def latest_type(self, *args, **kwargs):
            return []

    class Storage:
        work = Work()
        events = Events()

    project = release_cutoff.ProjectRelease(
        name="ameesh", repo="/fixture/repo", target="main",
        staging_executor="/fixture/executor.json")
    with mock.patch("ameesh.staging_executor.settings",
                    return_value={"key": "staging", "project": "ameesh"}), \
            mock.patch("ameesh.staging_executor.latest", return_value=request), \
            mock.patch.object(release_cutoff.storage, "of", return_value=Storage()), \
            mock.patch.object(release_cutoff, "tags", return_value=[tag()]):
        return release_cutoff.staging_holders(project, object())


class ReportErrorTest(unittest.TestCase):
    def report(self, *, exception, cutoff_tag=None, holders=(), model="semver"):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        cfg = config.Config(state_dir=folder.name)
        project = release_cutoff.ProjectRelease(
            name="ameesh", repo=folder.name, target="main", report="bureau",
            model=model)
        with mock.patch.object(release_cutoff, "compiled_rules",
                               return_value=(FakeRules(), SimpleNamespace(commit="c"))), \
                mock.patch.object(release_cutoff, "cutoff_slot",
                                  return_value=(100.0, "11:00")), \
                mock.patch.object(release_cutoff, "cutoff_tag",
                                  return_value=cutoff_tag), \
                mock.patch.object(release_cutoff, "staging_holders",
                                  return_value=list(holders)), \
                mock.patch.object(release_cutoff, "send_report") as send:
            release._report_error(cfg, object(), project, exception, now=110.0)
        return send

    def test_tagged_cutoff_whose_staging_is_running_is_information(self):
        holder = {"kind": "elected", "active": True,
                  "host": "ameesh-agents-1",
                  "execution": "run-1", "sha": SHA, "state": "running",
                  "version": "1.46.0", "tag": "v1.46.0"}
        send = self.report(
            exception=release_cutoff.ReleaseBusy(
                "staging is running (code 75, single-operator lock)"),
            cutoff_tag=tag(), holders=[holder])
        send.assert_called_once()
        body = send.call_args.args[3]
        self.assertIn("Coupure 1.46.0 faite par ameesh-agents-1", body)
        self.assertIn("tag v1.46.0", body)
        self.assertIn("Rien à corriger", body)
        self.assertNotIn("Aucun tag", body)
        self.assertIs(send.call_args.kwargs.get("urgent"), False)

    def test_the_same_information_is_mailed_once_per_cutoff(self):
        holder = {"kind": "elected", "active": True,
                  "host": "ameesh-agents-1",
                  "execution": "run-1", "sha": SHA, "state": "running",
                  "version": "1.46.0", "tag": "v1.46.0"}
        exception = release_cutoff.ReleaseBusy(
            "staging is running (code 75, single-operator lock)")
        # Two ticks in the same cutoff: one mail even though the state is
        # rewritten between the calls.
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        cfg = config.Config(state_dir=folder.name)
        project = release_cutoff.ProjectRelease(
            name="ameesh", repo=folder.name, target="main", report="bureau")
        with mock.patch.object(release_cutoff, "compiled_rules",
                               return_value=(FakeRules(), SimpleNamespace(commit="c"))), \
                mock.patch.object(release_cutoff, "cutoff_slot",
                                  return_value=(100.0, "11:00")), \
                mock.patch.object(release_cutoff, "cutoff_tag", return_value=tag()), \
                mock.patch.object(release_cutoff, "staging_holders",
                                  return_value=[holder]), \
                mock.patch.object(release_cutoff, "send_report") as send:
            release._report_error(cfg, object(), project, exception, now=110.0)
            release._report_error(cfg, object(), project, exception, now=111.0)
        send.assert_called_once()

    def test_tagged_cutoff_with_another_version_lock_alarms_and_names_it(self):
        holder = {"kind": "elected", "active": True,
                  "host": "ameesh-agents-2",
                  "execution": "run-2", "sha": OTHER, "state": "running",
                  "version": "1.45.0", "tag": "v1.45.0"}
        send = self.report(
            exception=release_cutoff.ReleaseBusy(
                "staging execution busy; request not replaced"),
            cutoff_tag=tag(), holders=[holder])
        send.assert_called_once()
        body = send.call_args.args[3]
        self.assertIn("coupure 11:00 déjà taguée", body)
        self.assertIn("hôte ameesh-agents-2", body)
        self.assertIn("exécution run-2", body)
        self.assertIn("version 1.45.0", body)
        self.assertIn("Aucun second tag", body)
        self.assertNotIn("Aucun tag, aucun déploiement", body)
        self.assertIs(send.call_args.kwargs.get("urgent"), True)

    def test_untagged_cutoff_still_alarms_and_names_the_orphan_holder(self):
        holder = {"kind": "elected", "host": "ameesh-agents-1",
                  "execution": "orphan-execution", "sha": "", "state": "held"}
        send = self.report(
            exception=release_cutoff.ReleaseBusy(
                "staging execution busy; request not replaced"),
            cutoff_tag=None, holders=[holder])
        send.assert_called_once()
        body = send.call_args.args[3]
        self.assertIn("Aucun tag, aucun déploiement", body)
        self.assertIn("hôte ameesh-agents-1", body)
        self.assertIn("exécution orphan-execution", body)
        self.assertIs(send.call_args.kwargs.get("urgent"), True)

    def test_terminal_request_at_the_tag_sha_does_not_mask_an_orphan_lease(self):
        # Review witness #6520: latest() is completed at the tag's commit
        # while an orphan execution still holds the lease. The tag must not
        # lend it a commit, and the orphan stays an urgent alarm.
        found = holders(
            {"request": "req-old", "state": "completed", "sha": SHA,
             "run": "old"},
            {"owner": "staging-execution:ameesh-agents-1:orphan",
             "expires_at": "2026-11-09T10:40:00Z"})
        self.assertEqual(len(found), 1)
        self.assertIs(found[0]["active"], False)
        self.assertEqual(found[0]["sha"], "")
        self.assertEqual(found[0]["state"], "held")
        send = self.report(
            exception=release_cutoff.ReleaseBusy(
                "staging execution busy; request not replaced"),
            cutoff_tag=tag(), holders=found)
        send.assert_called_once()
        body = send.call_args.args[3]
        self.assertIn("coupure 11:00 déjà taguée", body)
        self.assertIn("hôte ameesh-agents-1", body)
        self.assertIn("exécution orphan", body)
        self.assertIn("activité non prouvée", body)
        self.assertNotIn("Rien à corriger", body)
        self.assertNotIn("Aucun tag, aucun déploiement", body)
        self.assertNotIn("staging en cours", body)
        self.assertIs(send.call_args.kwargs.get("urgent"), True)

    def test_running_request_under_a_foreign_execution_alarms(self):
        # Review witness #6520: a running request is not this cutoff's
        # staging when it records another execution than the lease holder.
        found = holders(
            {"request": "req-1", "state": "running", "sha": SHA,
             "host": "ameesh-agents-2", "run": "run-1",
             "execution": "staging-execution:ameesh-agents-2:exec-2"},
            {"owner": "staging-execution:ameesh-agents-1:exec-1",
             "expires_at": "2026-11-09T10:40:00Z"})
        self.assertEqual(len(found), 1)
        self.assertIs(found[0]["active"], False)
        self.assertEqual(found[0]["sha"], "")
        self.assertEqual(found[0]["execution"], "exec-1")
        self.assertEqual(found[0]["host"], "ameesh-agents-1")
        send = self.report(
            exception=release_cutoff.ReleaseBusy(
                "staging execution busy; request not replaced"),
            cutoff_tag=tag(), holders=found)
        send.assert_called_once()
        self.assertIn("exécution exec-1", send.call_args.args[3])
        self.assertNotIn("Rien à corriger", send.call_args.args[3])
        self.assertIs(send.call_args.kwargs.get("urgent"), True)

    def test_local_release_lock_contention_is_not_a_cutoff_alarm(self):
        send = self.report(
            exception=release_cutoff.ReleaseBusy(
                "another release holds /state/release/release.lock"),
            cutoff_tag=None, holders=[])
        send.assert_not_called()


class ElectedHandoffTest(unittest.TestCase):
    def setUp(self):
        self.db = object()
        self.options = {"key": "staging", "project": "ameesh", "role": "pilote"}

    def run_staging(self, *, submit=None, latest=None, lease=None):
        project = release_cutoff.ProjectRelease(
            name="ameesh", repo="/fixture/repo", target="main",
            staging_executor="/fixture/executor.json")
        effect = ({"side_effect": submit} if isinstance(submit, BaseException)
                  else {"return_value": submit})
        with mock.patch("ameesh.staging_executor.settings",
                        return_value=self.options), \
                mock.patch("ameesh.staging_executor.submit", **effect), \
                mock.patch("ameesh.staging_executor.latest", return_value=latest), \
                mock.patch.object(release_cutoff, "_staging_execution_holder",
                                  return_value=lease), \
                mock.patch("ameesh.config.load", return_value=object()):
            return release_cutoff.run_staging(
                project, SHA, [], state_dir="/tmp/unused", db=self.db)

    def test_a_queued_elected_request_is_a_handoff_not_a_busy_lock(self):
        result = self.run_staging(submit={"request": "req-1", "state": "pending"})
        self.assertTrue(result["queued"])
        self.assertEqual(result["exit"], 75)
        self.assertEqual(result["request"], "req-1")
        self.assertEqual(result["request_state"], "pending")

    def test_execution_lease_busy_with_the_same_request_is_queued(self):
        owner = "staging-execution:ameesh-agents-1:exec-1"
        result = self.run_staging(
            submit=RuntimeError("staging execution busy; request not replaced"),
            latest={"request": "req-1", "state": "running", "sha": SHA,
                    "host": "ameesh-agents-1", "execution": owner},
            lease={"owner": owner})
        self.assertTrue(result["queued"])
        self.assertEqual(result["request"], "req-1")
        self.assertEqual(result["request_state"], "running")

    def test_execution_lease_busy_with_a_terminal_request_is_a_named_conflict(self):
        request = {"request": "req-old", "state": "completed", "sha": SHA,
                   "run": "old"}
        lease = {"owner": "staging-execution:ameesh-agents-1:orphan"}
        with mock.patch.object(release_cutoff, "staging_holders",
                               return_value=holders(request, lease)):
            with self.assertRaises(release_cutoff.ReleaseBusy) as caught:
                self.run_staging(
                    submit=RuntimeError(
                        "staging execution busy; request not replaced"),
                    latest=request, lease=lease)
        message = str(caught.exception)
        self.assertIn("hôte ameesh-agents-1", message)
        self.assertIn("exécution orphan", message)
        self.assertIn("activité non prouvée", message)
        self.assertIn("busy", message)

    def test_execution_lease_busy_with_a_foreign_execution_is_a_conflict(self):
        request = {"request": "req-1", "state": "running", "sha": SHA,
                   "host": "ameesh-agents-2", "run": "run-1",
                   "execution": "staging-execution:ameesh-agents-2:exec-2"}
        lease = {"owner": "staging-execution:ameesh-agents-1:exec-1"}
        with mock.patch.object(release_cutoff, "staging_holders",
                               return_value=holders(request, lease)):
            with self.assertRaises(release_cutoff.ReleaseBusy) as caught:
                self.run_staging(
                    submit=RuntimeError(
                        "staging execution busy; request not replaced"),
                    latest=request, lease=lease)
        message = str(caught.exception)
        self.assertIn("hôte ameesh-agents-1", message)
        self.assertIn("exécution exec-1", message)

    def test_execution_lease_busy_for_another_version_is_a_named_conflict(self):
        holder = {"kind": "elected", "host": "ameesh-agents-2",
                  "execution": "run-2", "sha": OTHER, "state": "running",
                  "version": "1.45.0", "tag": "v1.45.0"}
        with mock.patch.object(release_cutoff, "staging_holders",
                               return_value=[holder]):
            with self.assertRaises(release_cutoff.ReleaseBusy) as caught:
                self.run_staging(
                    submit=RuntimeError("staging execution busy; request not replaced"),
                    latest={"request": "req-2", "state": "running", "sha": OTHER,
                            "host": "ameesh-agents-2"})
        message = str(caught.exception)
        self.assertIn("hôte ameesh-agents-2", message)
        self.assertIn("exécution run-2", message)
        self.assertIn("version 1.45.0", message)

    def test_staging_step_does_not_raise_on_a_queued_request(self):
        queued = {"exit": 75, "queued": True, "request": "req-1",
                  "request_state": "pending", "request_host": "",
                  "duration_s": 0.0, "log": "", "summary": [], "attestation": None}
        project = release_cutoff.ProjectRelease(name="ameesh", repo="/fixture/repo")
        with mock.patch("ameesh.staging_pending.regressions", return_value=None), \
                mock.patch.object(release_cutoff, "maybe_staging", return_value=queued):
            result = release_cutoff._staging_step(
                project, SHA, [], "/tmp/unused", db=None)
        self.assertTrue(result["queued"])
        self.assertIsNone(result["exit"])


class HolderTest(unittest.TestCase):
    def test_elected_lease_request_and_sequence_host_are_named(self):
        class Work:
            def land_holder(self, key):
                return {"owner": "staging-execution:ameesh-agents-1:exec-1",
                        "expires_at": "2026-11-09T10:40:00Z"}

        class Events:
            def latest_type(self, *args, **kwargs):
                return [{"id": 1, "data": {
                    "run": "seq-1", "sha": OTHER, "state": "running",
                    "started": 90.0, "deadline": 200.0, "host": "pc-smichea",
                    "hosts": {}}}]

        class Storage:
            work = Work()
            events = Events()

        project = release_cutoff.ProjectRelease(
            name="ameesh", repo="/fixture/repo", target="main",
            staging_executor="/fixture/executor.json")
        rows = [tag(), {"version": "1.45.0", "tag": "v1.45.0",
                        "commit": OTHER, "at": 80.0}]
        with mock.patch("ameesh.staging_executor.settings",
                        return_value={"key": "staging", "project": "ameesh"}), \
                mock.patch("ameesh.staging_executor.latest",
                           return_value={"request": "req-1", "state": "running",
                                         "sha": SHA, "host": "ameesh-agents-1",
                                         "run": "seq-0",
                                         "execution": "staging-execution:ameesh-agents-1:exec-1"}), \
                mock.patch.object(release_cutoff.storage, "of", return_value=Storage()), \
                mock.patch.object(release_cutoff, "tags", return_value=rows):
            holders = release_cutoff.staging_holders(project, object())
        by_kind = {holder["kind"]: holder for holder in holders}
        self.assertIs(by_kind["elected"]["active"], True)
        self.assertEqual(by_kind["elected"]["host"], "ameesh-agents-1")
        self.assertEqual(by_kind["elected"]["execution"], "seq-0")
        self.assertEqual(by_kind["elected"]["version"], "1.46.0")
        self.assertEqual(by_kind["sequence"]["host"], "pc-smichea")
        self.assertEqual(by_kind["sequence"]["execution"], "seq-1")
        self.assertEqual(by_kind["sequence"]["version"], "1.45.0")

    def test_a_pending_handoff_without_lease_is_active(self):
        found = holders({"request": "req-1", "state": "pending", "sha": SHA},
                        None)
        self.assertEqual(len(found), 1)
        self.assertIs(found[0]["active"], True)
        self.assertEqual(found[0]["sha"], SHA)
        self.assertEqual(found[0]["state"], "pending")
        self.assertEqual(found[0]["version"], "1.46.0")

    def test_a_running_request_without_lease_is_named_but_inactive(self):
        found = holders(
            {"request": "req-1", "state": "running", "sha": SHA,
             "host": "ameesh-agents-1", "run": "run-1",
             "execution": "staging-execution:ameesh-agents-1:exec-1"}, None)
        self.assertEqual(len(found), 1)
        self.assertIs(found[0]["active"], False)
        self.assertEqual(found[0]["sha"], SHA)
        self.assertEqual(found[0]["state"], "running")
        self.assertIn("activité non prouvée",
                      release_cutoff.staging_holder_text(found[0]))

    def test_record_keeps_the_launcher_host_and_hides_an_unknown_one(self):
        base = {"run": "run-a", "sha": SHA, "state": "running",
                "started": 1.0, "deadline": 2.0, "hosts": {}}
        with mock.patch.object(staging_watch.storage, "of") as factory:
            staging_watch.record(None, dict(base, host=" ameesh-agents-1 "))
            saved = factory.return_value.events.record.call_args.args[3]
            self.assertEqual(saved["host"], "ameesh-agents-1")
            staging_watch.record(None, dict(base, host="  "))
            saved = factory.return_value.events.record.call_args.args[3]
            self.assertNotIn("host", saved)


class FinishTest(unittest.TestCase):
    def test_queued_staging_is_information_and_keeps_the_cutoff_open(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        cfg = config.Config(state_dir=folder.name)
        project = release_cutoff.ProjectRelease(
            name="ameesh", repo=folder.name, target="main", report="bureau")
        state = {"schema": release_cutoff.SCHEMA, "projects": {}}
        slot = release_cutoff.project_state(state, project.name)
        report = {"schema": release_cutoff.SCHEMA, "result": "reconciled",
                  "project": "ameesh", "version": "1.46.0", "tag": "v1.46.0",
                  "release_sha": SHA, "cutoff_at": 100.0, "label": "11:00",
                  "started_at": 95.0}
        queued = {"exit": None, "queued": True, "request": "req-1",
                  "request_state": "running", "request_host": "ameesh-agents-1",
                  "duration_s": 0.0, "log": "", "summary": [], "attestation": None}
        with mock.patch.object(release_cutoff, "send_report") as send:
            release_cutoff.finish(
                cfg, object(), project, report, state, slot, 100.0, "11:00",
                FakeRules(), None, None, queued, False)
            release_cutoff.finish(
                cfg, object(), project, dict(report), state, slot, 100.0, "11:00",
                FakeRules(), None, None, queued, False)
        send.assert_called_once()
        self.assertIs(send.call_args.kwargs.get("urgent"), False)
        self.assertIn("staging en cours", send.call_args.args[3])
        self.assertIn("Coupure 1.46.0 faite", send.call_args.args[3])
        self.assertNotIn("last_cutoff", slot)
        self.assertTrue(report["staging_pending"])
        self.assertFalse(report["staging_ok"])

        # The attestation lands: the next reconciliation closes the cutoff.
        attested = {"exit": 0, "duration_s": 1.0, "log": "/tmp/log",
                    "summary": [], "attestation": {"sha": SHA}}
        with mock.patch.object(release_cutoff, "send_report") as send:
            release_cutoff.finish(
                cfg, object(), project, dict(report), state, slot, 100.0, "11:00",
                FakeRules(), None, None, attested, False)
        self.assertEqual(slot["last_cutoff"], 100.0)
        self.assertIs(send.call_args.kwargs.get("urgent"), False)


class TwoTicksTest(ReleaseFixtureMixin, unittest.TestCase):
    """A second host reconciles the first tag and only informs."""

    def test_second_tick_queues_staging_without_a_second_tag(self):
        first = self.execute()
        self.assertEqual(first["result"], "released", first)
        release_cutoff.save_state(self.cfg, {"schema": release_cutoff.SCHEMA,
                                             "projects": {}})
        tag_at = float(self.git(
            self.clone, "for-each-ref", "--format=%(taggerdate:unix)",
            "refs/tags/v1.11.8"))
        queued = {"exit": 75, "queued": True, "request": "req-1",
                  "request_state": "running", "request_host": "ameesh-agents-1",
                  "duration_s": 0.0, "log": "", "summary": [], "attestation": None}
        with mock.patch.object(release_cutoff, "maybe_staging",
                               return_value=queued):
            second = self.execute(now=tag_at + 10)
        self.assertEqual(second["result"], "reconciled", second)
        self.assertTrue(second["staging_pending"])
        self.assertEqual(self.git(self.clone, "tag", "--list", "v1.11.8"),
                         "v1.11.8")
        slot = release_cutoff.load_state(self.cfg)["projects"]["ameesh"]
        self.assertNotIn("last_cutoff", slot)
        attested = {"exit": 0, "duration_s": 1.0, "log": "/tmp/log",
                    "summary": [], "attestation": {"sha": first["release_sha"]}}
        with mock.patch.object(release_cutoff, "maybe_staging",
                               return_value=attested):
            third = self.execute(now=tag_at + 20)
        self.assertEqual(third["result"], "reconciled", third)
        slot = release_cutoff.load_state(self.cfg)["projects"]["ameesh"]
        self.assertEqual(slot["last_cutoff"], _paris(11))
        self.assertEqual(self.git(self.clone, "tag", "--list", "v1.11.8"),
                         "v1.11.8")


if __name__ == "__main__":
    unittest.main()
