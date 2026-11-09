# SPDX-License-Identifier: AGPL-3.0-only
"""#6584: the release tick reads an N-1 staging request and its live sequence.

The 1.47.0 cutoff of 09/11 alarmed every minute while the staging sequence
was progressing: the executor of AG2, still on 1.46.0, wrote its request
without the `execution` field, the AG1 tick read it inactive, and
`staging_holders` dropped the live sequence because an inactive entry of the
same (execution, host) was already there. The same alarm repeated because the
de-duplication compared a text carrying the lease's renewed `expires_at`.

These tests replay the N-1 writer's payload (`previous_write` in the sense of
CONTRIBUTING §« Compatibilité N-1 »: the old writer ignores the new field),
the live sequence merge, the true orphan that must still alarm, and the
one-mail-per-holder-identity rule across a renewed lease.
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
RUN = "d710fa6b39134d97abf66ed825c933ba"
OWNER = "staging-execution:ameesh-agents-2:9ff7506c808b403dbf190de365ef89af"
AG2 = "ameesh-agents-2"

#: Exact payload of an N-1 writer (`staging_executor._record`): it names its
#: host and run but ignores the `execution` field introduced by #6520.
PREVIOUS_WRITE = {"request": "100e73cb4cf140ab85988e452fbf84a7",
                  "state": "running", "sha": SHA, "host": AG2, "run": RUN}


def sequence(run=RUN, sha=SHA, host=AG2, state="running"):
    """One shared `staging.sequence` snapshot, as the launcher records it."""
    return {"run": run, "sha": sha, "state": state, "started": 1794261870.0,
            "deadline": 1794301470.0, "host": host, "hosts": {}}


def holders(request, lease, sequences=()):
    """Run `staging_holders` against a fake shared DB (one elected key)."""
    class Work:
        def land_holder(self, key):
            return lease

    class Events:
        def latest_type(self, *args, **kwargs):
            return [{"id": i, "data": data}
                    for i, data in enumerate(sequences, 1)]

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
            mock.patch.object(release_cutoff, "tags",
                              return_value=[tag(), tag("1.45.0", OTHER)]):
        return release_cutoff.staging_holders(project, object())


class HolderTest(unittest.TestCase):
    def test_previous_write_running_request_attaches_by_host_and_live_run(self):
        # The exact 22:23Z shape: AG2's running request has no execution, the
        # lease is AG2's, and the sequence of its run is live on the tag SHA.
        found = holders(PREVIOUS_WRITE,
                        {"owner": OWNER,
                         "expires_at": "2026-11-09T22:31:09.846797+00:00"},
                        [sequence()])
        self.assertEqual(len(found), 1, found)
        holder = found[0]
        self.assertIs(holder["active"], True)
        self.assertEqual(holder["host"], AG2)
        self.assertEqual(holder["execution"], RUN)
        self.assertEqual(holder["sha"], SHA)
        self.assertEqual(holder["state"], "running")
        self.assertEqual(holder["version"], "1.46.0")
        self.assertEqual(holder["tag"], "v1.46.0")

    def test_previous_write_running_request_without_lease_is_active(self):
        # "à défaut par la séquence en vol": the shared sequence is the proof,
        # even before the lease is read.
        found = holders(PREVIOUS_WRITE, None, [sequence()])
        self.assertEqual(len(found), 1, found)
        self.assertIs(found[0]["active"], True)
        self.assertEqual(found[0]["sha"], SHA)

    def test_previous_write_without_a_live_sequence_leaves_an_orphan(self):
        found = holders(PREVIOUS_WRITE,
                        {"owner": OWNER,
                         "expires_at": "2026-11-09T22:31:09.846797+00:00"},
                        [])
        self.assertEqual(len(found), 2, found)
        self.assertTrue(all(holder["active"] is False for holder in found))
        by_sha = {holder["sha"]: holder for holder in found}
        self.assertEqual(by_sha[""]["execution"],
                         "9ff7506c808b403dbf190de365ef89af")
        self.assertEqual(by_sha[""]["state"], "held")
        self.assertEqual(by_sha[SHA]["execution"], RUN)
        self.assertEqual(by_sha[SHA]["state"], "running")

    def test_previous_write_live_sequence_of_another_commit_does_not_prove_it(self):
        found = holders(PREVIOUS_WRITE, None,
                        [sequence(sha=OTHER)])
        self.assertEqual(len(found), 2, found)
        active = [holder for holder in found if holder["active"]]
        self.assertEqual([holder["sha"] for holder in active], [OTHER])
        by_sha = {holder["sha"]: holder for holder in found}
        self.assertIs(by_sha[SHA]["active"], False)
        self.assertEqual(by_sha[SHA]["state"], "running")


class ReportTest(unittest.TestCase):
    """`release._report_error`: what the tick mails, and only once."""

    def cfg(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        return config.Config(state_dir=folder.name)

    def report(self, *, exception, cfg, cutoff_tag=None, holders=(), now=110.0,
               side_effect=None):
        project = release_cutoff.ProjectRelease(
            name="ameesh", repo=cfg.state_dir, target="main", report="bureau")
        effect = ({"side_effect": side_effect} if side_effect is not None
                  else {"return_value": list(holders)})
        with mock.patch.object(release_cutoff, "compiled_rules",
                               return_value=(FakeRules(), SimpleNamespace(commit="c"))), \
                mock.patch.object(release_cutoff, "cutoff_slot",
                                  return_value=(100.0, "11:00")), \
                mock.patch.object(release_cutoff, "cutoff_tag",
                                  return_value=cutoff_tag), \
                mock.patch.object(release_cutoff, "staging_holders", **effect), \
                mock.patch.object(release_cutoff, "send_report") as send:
            release._report_error(cfg, object(), project, exception, now=now)
        return send

    def test_a_previous_writer_with_a_live_sequence_is_information(self):
        request = dict(PREVIOUS_WRITE)
        found = holders(request,
                        {"owner": OWNER,
                         "expires_at": "2026-11-09T22:31:09.846797+00:00"},
                        [sequence()])
        send = self.report(
            cfg=self.cfg(),
            exception=release_cutoff.ReleaseBusy(
                "staging execution busy; request not replaced"),
            cutoff_tag=tag(), holders=found)
        send.assert_called_once()
        body = send.call_args.args[3]
        self.assertIn("Coupure 1.46.0 faite par ameesh-agents-2", body)
        self.assertIn("tag v1.46.0", body)
        self.assertIn("Staging en cours", body)
        self.assertIn("Rien à corriger", body)
        self.assertNotIn("Aucun second tag", body)
        self.assertIs(send.call_args.kwargs.get("urgent"), False)

    def test_a_true_orphan_lease_is_still_an_urgent_alarm(self):
        found = holders(PREVIOUS_WRITE,
                        {"owner": OWNER,
                         "expires_at": "2026-11-09T22:31:09.846797+00:00"},
                        [])
        send = self.report(
            cfg=self.cfg(),
            exception=release_cutoff.ReleaseBusy(
                "staging execution busy; request not replaced"),
            cutoff_tag=tag(), holders=found)
        send.assert_called_once()
        body = send.call_args.args[3]
        self.assertIn("déjà taguée", body)
        self.assertIn("activité non prouvée", body)
        self.assertIn("Aucun second tag", body)
        self.assertNotIn("Rien à corriger", body)
        self.assertIs(send.call_args.kwargs.get("urgent"), True)

    def test_a_renewed_orphan_lease_mails_once(self):
        self.run_renewed(active=False)

    def test_a_renewed_information_lease_mails_once(self):
        self.run_renewed(active=True)

    def run_renewed(self, *, active):
        cfg = self.cfg()
        request = dict(PREVIOUS_WRITE)
        first = ({"kind": "elected", "active": True, "host": AG2,
                  "execution": RUN, "sha": SHA, "state": "running",
                  "version": "1.46.0", "tag": "v1.46.0"}
                 if active else
                 {"kind": "elected", "active": False, "host": AG2,
                  "execution": "9ff7506c808b403dbf190de365ef89af",
                  "sha": "", "state": "held"})
        first["expires_at"] = "2026-11-09T22:31:09.846797+00:00"
        second = dict(first, expires_at="2026-11-09T22:32:09.846797+00:00")
        exceptions = [
            release_cutoff.ReleaseBusy(
                "staging execution busy; request not replaced (détenteur : %s)"
                % release_cutoff.staging_holders_text([holder]))
            for holder in (first, second)
        ]
        send = self.report(
            cfg=cfg, exception=exceptions[0], cutoff_tag=tag(),
            side_effect=[[first], [second]], now=110.0)
        send2 = self.report(
            cfg=cfg, exception=exceptions[1], cutoff_tag=tag(),
            side_effect=[[second]], now=170.0)
        # One mail for both ticks: the renewed expiry is not a new holder.
        self.assertEqual(send.call_count + send2.call_count, 1)
        called = send if send.call_count else send2
        self.assertIs(called.call_args.kwargs.get("urgent"), not active)


class ElectedHandoffTest(unittest.TestCase):
    """`run_staging`: the N-1 request is a queued handoff, not a busy lock."""

    def run_staging(self, *, sequences):
        project = release_cutoff.ProjectRelease(
            name="ameesh", repo="/fixture/repo", target="main",
            staging_executor="/fixture/executor.json")
        with mock.patch("ameesh.staging_executor.settings",
                        return_value={"key": "staging", "project": "ameesh"}), \
                mock.patch("ameesh.staging_executor.submit",
                           side_effect=RuntimeError(
                               "staging execution busy; request not replaced")), \
                mock.patch("ameesh.staging_executor.latest",
                           return_value=dict(PREVIOUS_WRITE)), \
                mock.patch.object(release_cutoff, "_staging_execution_holder",
                                  return_value={"owner": OWNER}), \
                mock.patch.object(release_cutoff, "_staging_sequences",
                                  return_value=sequences), \
                mock.patch("ameesh.config.load", return_value=object()):
            return release_cutoff.run_staging(
                project, SHA, [], state_dir="/tmp/unused", db=object())

    def test_previous_writer_with_a_live_sequence_is_a_queued_handoff(self):
        result = self.run_staging(sequences={RUN: sequence()})
        self.assertTrue(result["queued"])
        self.assertEqual(result["exit"], 75)
        self.assertEqual(result["request"], PREVIOUS_WRITE["request"])
        self.assertEqual(result["request_state"], "running")

    def test_previous_writer_without_a_sequence_is_busy(self):
        with self.assertRaises(release_cutoff.ReleaseBusy):
            self.run_staging(sequences={})


if __name__ == "__main__":
    unittest.main()
