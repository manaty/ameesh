# SPDX-License-Identifier: AGPL-3.0-only
"""#6992: the start window of an N-1 staging request is not a busy alarm.

The 1.50.0 cutoff of 11/11 tagged 3c546ab4 at 22:02:53Z. The AG1 executor
(sealed venv of 08/11, N-1) then took the request and held the execution
lease while it probed and prepared. At 22:04:31Z, before its `running`
snapshot (written at 22:04:34Z), the next release tick found the lease held
and the shared sequence log still empty. `_staging_request_active` read the
pending request (no `execution` field, N-1 writer), then the running one, as
inactive, so the tick mailed the urgent "staging execution busy" alarm
although the staging sequence was starting normally.

Witnesses: an N-1 pending request inside its start deadline, or a running one
on the lease holder's host within the executor's start window, is the
cutoff's staging (information, not urgent); another commit, another host, an
expired window, or a blocked request still alarms; a live sequence keeps its
previous reading.
"""
from __future__ import annotations

import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import config, release, release_cutoff
from tests.test_6520_release_staging_busy import FakeRules, tag

SHA = "a" * 40
OTHER = "b" * 40
RUN = "aac4c2f9c0de45e8a8f6e8d5f0b9a1c2"
AG1 = "ameesh-agents-1"
AG2 = "ameesh-agents-2"
OWNER = "staging-execution:ameesh-agents-1:9ff7506c808b403dbf190de365ef89af"
STARTED = 1794261870.0
NOW = STARTED + 10.0

#: Exact payload of an N-1 writer (`staging_executor.tick`): it names its
#: host, run and attempt start but ignores the `execution` field of #6520.
PREVIOUS_WRITE = {"request": "100e73cb4cf140ab85988e452fbf84a7",
                  "state": "running", "sha": SHA, "host": AG1, "run": RUN,
                  "started": STARTED}

#: Exact payload of its queued handoff (`staging_executor.submit`): no host,
#: no run and no `execution`, but the start deadline it was submitted with.
PENDING_WRITE = {"request": "fe74ff3705804adeb06368146b78678b",
                 "state": "pending", "sha": SHA, "included": [],
                 "notify": "release", "deadline": STARTED + 300.0}


def request(**fields):
    data = dict(PREVIOUS_WRITE)
    data.update(fields)
    return data


def pending(**fields):
    data = dict(PENDING_WRITE)
    data.update(fields)
    return data


def lease(owner=OWNER):
    return {"owner": owner, "expires_at": "2026-11-11T22:09:31.846797+00:00"}


def sequence(run=RUN, sha=SHA, host=AG1, state="running"):
    """One shared `staging.sequence` snapshot, as the launcher records it."""
    return {"run": run, "sha": sha, "state": state, "started": STARTED,
            "deadline": STARTED + 3600.0, "host": host, "hosts": {}}


class FixedClock:
    """#6992: the reader's clock is fixed so both windows are exact."""

    @staticmethod
    def time():
        return NOW


def holders(data, held, sequences=(), *, start_timeout_s=300.0):
    """Run `staging_holders` against a fake shared DB (one elected key)."""
    class Work:
        def land_holder(self, key):
            return held

    class Events:
        def latest_type(self, *args, **kwargs):
            return [{"id": i, "data": row}
                    for i, row in enumerate(sequences, 1)]

    class Storage:
        work = Work()
        events = Events()

    project = release_cutoff.ProjectRelease(
        name="ameesh", repo="/fixture/repo", target="main",
        staging_executor="/fixture/executor.json")
    with mock.patch("ameesh.staging_executor.settings",
                    return_value={"key": "staging", "project": "ameesh",
                                  "start_timeout_s": start_timeout_s}), \
            mock.patch("ameesh.staging_executor.latest", return_value=data), \
            mock.patch.object(release_cutoff, "time", FixedClock), \
            mock.patch.object(release_cutoff.storage, "of", return_value=Storage()), \
            mock.patch.object(release_cutoff, "tags",
                              return_value=[tag(), tag("1.45.0", OTHER)]):
        return release_cutoff.staging_holders(project, object())


class StartWindowTest(unittest.TestCase):
    """`_staging_request_active`: the window before the first sequence."""

    def active(self, data, held=OWNER, sequences=None, **kwargs):
        lease_data = None if held is None else lease(held)
        return release_cutoff._staging_request_active(
            data, lease_data, sequences if sequences is not None else {},
            start_timeout_s=300.0, now=NOW, **kwargs)

    def test_a_running_n1_request_in_the_start_window_is_active(self):
        self.assertIs(self.active(request()), True)

    def test_a_pending_n1_request_inside_its_deadline_is_active(self):
        self.assertIs(self.active(pending()), True)

    def test_a_pending_n1_request_past_its_deadline_is_not_active(self):
        self.assertIs(self.active(pending(deadline=NOW - 1.0)), False)

    def test_a_pending_n1_request_without_a_deadline_is_not_active(self):
        data = pending()
        data.pop("deadline")
        self.assertIs(self.active(data), False)

    def test_a_pending_new_writer_stays_under_its_execution(self):
        self.assertIs(self.active(pending(execution=OWNER)), True)
        self.assertIs(self.active(pending(execution="staging-execution:x:y")),
                      False)

    def test_a_pending_handoff_without_a_lease_is_active(self):
        self.assertIs(self.active(pending(), held=None), True)

    def test_the_window_is_the_executor_start_timeout(self):
        # NOW is STARTED + 10: exactly 300 s old is still inside the window.
        self.assertIs(self.active(request(started=STARTED - 290.0)), True)
        self.assertIs(self.active(request(started=STARTED + 11.0)), False)
        self.assertIs(self.active(request(started=STARTED - 301.0)), False)

    def test_another_host_than_the_held_lease_is_not_active(self):
        self.assertIs(self.active(request(host=AG2)), False)

    def test_a_window_without_a_held_lease_is_not_active(self):
        self.assertIs(self.active(request(), held=None), False)

    def test_a_window_without_the_attempt_start_is_not_active(self):
        data = request()
        data.pop("started")
        self.assertIs(self.active(data), False)

    def test_a_blocked_request_ahead_of_the_window_still_alarms(self):
        self.assertIs(self.active(request(blocked_reason="manifest")), False)

    def test_a_live_sequence_of_another_commit_still_alarms(self):
        self.assertIs(self.active(request(), sequences={RUN: sequence(sha=OTHER)}),
                      False)

    def test_a_live_sequence_keeps_its_previous_reading(self):
        # Past the start window, the sequence of its own run is the proof.
        held = lease()
        self.assertIs(
            release_cutoff._staging_request_active(
                request(), held, {RUN: sequence()},
                start_timeout_s=300.0, now=STARTED + 4000.0),
            True)

    def test_a_caller_without_the_window_keeps_the_pre_6992_rule(self):
        self.assertIs(
            release_cutoff._staging_request_active(
                request(), lease(), {}, now=NOW),
            False)


class HoldersTest(unittest.TestCase):
    """`staging_holders`: the attempt's first seconds are staging."""

    def test_the_start_window_request_is_one_active_holder(self):
        found = holders(request(started=NOW - 10.0), lease())
        self.assertEqual(len(found), 1, found)
        self.assertIs(found[0]["active"], True)
        self.assertEqual(found[0]["host"], AG1)
        self.assertEqual(found[0]["execution"], RUN)
        self.assertEqual(found[0]["sha"], SHA)
        self.assertEqual(found[0]["state"], "running")

    def test_past_the_window_the_lease_is_named_as_an_orphan(self):
        found = holders(request(started=NOW - 301.0), lease())
        self.assertEqual(len(found), 2, found)
        self.assertTrue(all(holder["active"] is False for holder in found))
        by_sha = {holder["sha"]: holder for holder in found}
        self.assertEqual(by_sha[""]["state"], "held")
        self.assertEqual(by_sha[SHA]["state"], "running")

    def test_a_pending_handoff_is_one_active_holder(self):
        found = holders(pending(deadline=NOW + 290.0), lease())
        self.assertEqual(len(found), 1, found)
        self.assertIs(found[0]["active"], True)
        self.assertEqual(found[0]["host"], AG1)
        self.assertEqual(found[0]["execution"],
                         "9ff7506c808b403dbf190de365ef89af")
        self.assertEqual(found[0]["sha"], SHA)
        self.assertEqual(found[0]["state"], "pending")

    def test_a_pending_handoff_past_its_deadline_leaves_an_orphan(self):
        found = holders(pending(deadline=NOW - 1.0), lease())
        self.assertEqual(len(found), 2, found)
        self.assertTrue(all(holder["active"] is False for holder in found))

    def test_a_blocked_request_ahead_of_the_window_is_never_active(self):
        found = holders(request(started=NOW - 10.0,
                                blocked_reason="manifest"), lease())
        self.assertTrue(all(holder["active"] is False for holder in found))
        self.assertTrue(any(holder.get("blocked_reason") == "manifest"
                            for holder in found), found)


class ElectedHandoffTest(unittest.TestCase):
    """`run_staging`: the lease holder's fresh attempt is a queued handoff."""

    def run_staging(self, data, *, sequences=None, start_timeout_s=300.0):
        project = release_cutoff.ProjectRelease(
            name="ameesh", repo="/fixture/repo", target="main",
            staging_executor="/fixture/executor.json")
        with mock.patch("ameesh.staging_executor.settings",
                        return_value={"key": "staging", "project": "ameesh",
                                      "start_timeout_s": start_timeout_s}), \
                mock.patch("ameesh.staging_executor.submit",
                           side_effect=RuntimeError(
                               "staging execution busy; request not replaced")), \
                mock.patch("ameesh.staging_executor.latest", return_value=data), \
                mock.patch.object(release_cutoff, "_staging_execution_holder",
                                  return_value=lease()), \
                mock.patch.object(release_cutoff, "_staging_sequences",
                                  return_value={} if sequences is None else sequences), \
                mock.patch.object(release_cutoff, "time", FixedClock), \
                mock.patch("ameesh.config.load", return_value=object()):
            return release_cutoff.run_staging(
                project, SHA, [], state_dir="/tmp/unused", db=object())

    def test_the_start_window_request_is_a_queued_handoff(self):
        result = self.run_staging(request(started=NOW - 10.0))
        self.assertTrue(result["queued"])
        self.assertEqual(result["exit"], 75)
        self.assertEqual(result["request_state"], "running")

    def test_a_pending_handoff_is_a_queued_handoff(self):
        result = self.run_staging(pending(deadline=NOW + 290.0))
        self.assertTrue(result["queued"])
        self.assertEqual(result["exit"], 75)
        self.assertEqual(result["request_state"], "pending")

    def test_a_pending_handoff_past_its_deadline_is_still_busy(self):
        with self.assertRaises(release_cutoff.ReleaseBusy):
            self.run_staging(pending(deadline=NOW - 1.0))

    def test_a_pending_handoff_of_another_commit_is_still_busy(self):
        with self.assertRaises(release_cutoff.ReleaseBusy):
            self.run_staging(pending(sha=OTHER, deadline=NOW + 290.0))

    def test_another_commit_is_still_busy(self):
        with self.assertRaises(release_cutoff.ReleaseBusy):
            self.run_staging(request(sha=OTHER, started=NOW - 10.0))

    def test_a_request_past_the_window_is_still_busy(self):
        with self.assertRaises(release_cutoff.ReleaseBusy):
            self.run_staging(request(started=NOW - 301.0))

    def test_a_blocked_request_is_still_busy(self):
        with self.assertRaises(release_cutoff.ReleaseBusy):
            self.run_staging(request(started=NOW - 10.0,
                                     blocked_reason="manifest"))


class ReportTest(unittest.TestCase):
    """`release._report_error`: the window is information, never urgency."""

    def cfg(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        return config.Config(state_dir=folder.name)

    def report(self, *, exception, cfg, cutoff_tag=None, holders=(), now=110.0):
        project = release_cutoff.ProjectRelease(
            name="ameesh", repo=cfg.state_dir, target="main", report="bureau")
        with mock.patch.object(release_cutoff, "compiled_rules",
                               return_value=(FakeRules(), SimpleNamespace(commit="c"))), \
                mock.patch.object(release_cutoff, "cutoff_slot",
                                  return_value=(100.0, "11:00")), \
                mock.patch.object(release_cutoff, "cutoff_tag",
                                  return_value=cutoff_tag), \
                mock.patch.object(release_cutoff, "staging_holders",
                                  return_value=list(holders)), \
                mock.patch.object(release_cutoff, "send_report") as send:
            release._report_error(cfg, object(), project, exception, now=now)
        return send

    def test_the_start_window_is_information_not_an_urgent_alarm(self):
        found = holders(request(started=NOW - 10.0), lease())
        send = self.report(
            cfg=self.cfg(),
            exception=release_cutoff.ReleaseBusy(
                "staging execution busy; request not replaced"),
            cutoff_tag=tag(), holders=found)
        send.assert_called_once()
        body = send.call_args.args[3]
        self.assertIn("Coupure 1.46.0 faite", body)
        self.assertIn("tag v1.46.0", body)
        self.assertIn("Staging en cours", body)
        self.assertIn("Rien à corriger", body)
        self.assertIs(send.call_args.kwargs.get("urgent"), False)

    def test_a_pending_handoff_is_information_not_an_urgent_alarm(self):
        found = holders(pending(deadline=NOW + 290.0), lease())
        send = self.report(
            cfg=self.cfg(),
            exception=release_cutoff.ReleaseBusy(
                "staging execution busy; request not replaced"),
            cutoff_tag=tag(), holders=found)
        send.assert_called_once()
        body = send.call_args.args[3]
        self.assertIn("Staging en attente", body)
        self.assertIn("Rien à corriger", body)
        self.assertIs(send.call_args.kwargs.get("urgent"), False)

    def test_a_pending_handoff_of_another_commit_is_an_urgent_alarm(self):
        found = holders(pending(sha=OTHER, deadline=NOW + 290.0),
                        lease())
        send = self.report(
            cfg=self.cfg(),
            exception=release_cutoff.ReleaseBusy(
                "staging execution busy; request not replaced"),
            cutoff_tag=tag(), holders=found)
        send.assert_called_once()
        self.assertIn("déjà taguée", send.call_args.args[3])
        self.assertIs(send.call_args.kwargs.get("urgent"), True)

    def test_another_commit_in_the_window_is_still_an_urgent_alarm(self):
        found = holders(request(sha=OTHER, started=NOW - 10.0), lease())
        send = self.report(
            cfg=self.cfg(),
            exception=release_cutoff.ReleaseBusy(
                "staging execution busy; request not replaced"),
            cutoff_tag=tag(), holders=found)
        send.assert_called_once()
        self.assertIn("déjà taguée", send.call_args.args[3])
        self.assertIs(send.call_args.kwargs.get("urgent"), True)

    def test_a_blocked_request_in_the_window_is_still_an_urgent_alarm(self):
        found = holders(request(started=NOW - 10.0,
                                blocked_reason="manifest"), lease())
        send = self.report(
            cfg=self.cfg(),
            exception=release_cutoff.ReleaseBusy(
                "staging execution busy; request not replaced"),
            cutoff_tag=tag(), holders=found)
        send.assert_called_once()
        self.assertIn("bloqué", send.call_args.args[3])
        self.assertIs(send.call_args.kwargs.get("urgent"), True)


if __name__ == "__main__":
    unittest.main()
