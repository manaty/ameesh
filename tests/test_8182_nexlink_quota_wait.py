# SPDX-License-Identifier: AGPL-3.0-only
"""#8182: a GitHub quota 403 makes the Nexlink cut wait for the reset.

The incident: `release tick` retried the Nexlink cutoff every minute while
GitHub answered `HTTP 403: API rate limit exceeded ... request ID ... and
timestamp ...`. The request ID changed at every attempt, so the per-text
de-duplication never matched and every tick mailed an urgent event to the
pilot, for a quota that only recharged at 22:52Z.

A quota rejection now stores the reset read by the shared detector
(`plan_github.SharedReads`), sends one non-urgent notice with the resume time,
and the next ticks return `waiting-quota` before touching the engine. A 403
that is not a quota keeps the ordinary error path.
"""
from __future__ import annotations

import os
import tempfile
import types
import unittest
from unittest import mock

from tests import support  # isolate host config before product imports
from ameesh import config
from ameesh import release
from ameesh import release_cutoff as rc

QUOTA = ("Nexlink GitHub operation failed: HTTP 403: API rate limit exceeded "
         "for user ID 16659140. If you reach out to GitHub Support for help, "
         "please include the request ID 995A:2A14FC:C41606:128DC8C:6B04C2C8 "
         "and timestamp 2026-11-23 22:48:40 UTC.")
RIGHTS = ("Nexlink GitHub operation failed: HTTP 403: Resource not accessible "
          "by integration")


def quota_probe(reset: float):
    """The 200 `/rate_limit` probe already observed on the release host."""
    body = ('{"resources": {"core": {"limit": 5000, "used": 5000, '
            '"remaining": 0, "reset": %d}}}' % int(reset))
    return 200, {"X-RateLimit-Limit": "5000", "X-RateLimit-Remaining": "0",
                 "X-RateLimit-Reset": str(int(reset)),
                 "X-RateLimit-Resource": "core"}, body


def healthy_probe(reset: float = 1200.0):
    """A recharged bucket: the same headers, remaining left."""
    body = ('{"resources": {"core": {"limit": 5000, "used": 100, '
            '"remaining": 4900, "reset": %d}}}' % int(reset))
    return 200, {"X-RateLimit-Limit": "5000", "X-RateLimit-Remaining": "4900",
                 "X-RateLimit-Reset": str(int(reset)),
                 "X-RateLimit-Resource": "core"}, body


class QuotaDetectorTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.temp.cleanup)
        self.cfg = config.Config(state_dir=self.temp.name)

    def test_the_reset_comes_from_the_shared_quota_detector(self):
        with mock.patch.object(release, "_quota_probe",
                               return_value=quota_probe(1200.0)):
            until = release._quota_wait_until(self.cfg, QUOTA, 1000.0)
        self.assertEqual(until, 1200.0)
        # The detector's own state now refuses the next read without network.
        with mock.patch.object(release, "_quota_probe") as probe:
            self.assertEqual(
                release._quota_wait_until(self.cfg, QUOTA, 1010.0), 1200.0)
            probe.assert_not_called()

    def test_a_rights_403_is_not_a_quota(self):
        with mock.patch.object(release, "_quota_probe",
                               return_value=healthy_probe()) as probe:
            self.assertIsNone(release._quota_wait_until(self.cfg, RIGHTS, 1000.0))
            probe.assert_called_once()

    def test_a_403_is_graded_by_the_headers_not_the_wording(self):
        # A rights-looking message while the bucket is empty is a quota wait;
        # a quota-looking message while the bucket is full is not. Each case
        # gets its own quota state, as a probe writes the observed reset.
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as folder, \
                mock.patch.object(release, "_quota_probe",
                                  return_value=quota_probe(1200.0)):
            self.assertEqual(
                release._quota_wait_until(
                    config.Config(state_dir=folder), RIGHTS, 1000.0),
                1200.0)
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as folder, \
                mock.patch.object(release, "_quota_probe",
                                  return_value=healthy_probe()):
            self.assertIsNone(
                release._quota_wait_until(
                    config.Config(state_dir=folder), QUOTA, 1000.0))

    def test_an_unreadable_probe_does_not_invent_a_reset(self):
        with mock.patch.object(release, "_quota_probe",
                               side_effect=OSError("no gh")):
            self.assertIsNone(release._quota_wait_until(self.cfg, QUOTA, 1000.0))

    def test_a_429_with_a_proven_reset_is_a_quota(self):
        with mock.patch.object(release, "_quota_probe",
                               return_value=quota_probe(1300.0)):
            self.assertEqual(
                release._quota_wait_until(
                    self.cfg, "gh: HTTP 429: Too Many Requests", 1000.0),
                1300.0)


class QuotaReportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.temp.cleanup)
        self.cfg = config.Config(state_dir=self.temp.name)
        self.project = rc.ProjectRelease(
            "nexlink", self.temp.name, target="develop", report="bureau",
            model="nexlink")
        rc.save_state(self.cfg, {"schema": rc.SCHEMA, "projects": {"nexlink": {}}})

    def _report(self, text, now, probe):
        with mock.patch.object(rc, "compiled_rules",
                               return_value=(None, types.SimpleNamespace(commit="d" * 40))), \
                mock.patch.object(rc, "cutoff_slot", return_value=(1000.0, "23:00")), \
                mock.patch.object(release, "_quota_probe", side_effect=probe), \
                mock.patch.object(rc, "send_report") as send:
            waited = release._report_error(
                self.cfg, object(), self.project,
                rc.TransientReleaseError(text), now)
        return waited, send

    def test_quota_403_waits_and_alerts_once_without_urgency(self):
        waited, first = self._report(QUOTA, 1000.0, lambda: quota_probe(1200.0))
        self.assertTrue(waited)
        self.assertEqual(first.call_count, 1)
        self.assertFalse(first.call_args.kwargs["urgent"])
        slot = rc.load_state(self.cfg)["projects"]["nexlink"]
        self.assertEqual(slot["quota_wait_reset"], 1200.0)
        self.assertEqual(slot["quota_wait_until"], 1260.0)
        self.assertIn("00:21:00Z", first.call_args.args[3])
        self.assertEqual(first.call_args.args[4]["result"], "waiting-quota")

        # A second failure in the same window (another request ID) is not a
        # second alert, even if it is reported again.
        waited, second = self._report(QUOTA.replace("995A", "ABCD"),
                                      1010.0, lambda: quota_probe(1200.0))
        self.assertTrue(waited)
        second.assert_not_called()

    def test_rights_403_stays_an_urgent_error(self):
        waited, send = self._report(RIGHTS, 1000.0, lambda: healthy_probe())
        self.assertFalse(waited)
        self.assertEqual(send.call_count, 1)
        self.assertTrue(send.call_args.kwargs["urgent"])
        self.assertEqual(send.call_args.args[4]["result"], "refused")
        self.assertNotIn("quota_wait_until",
                         rc.load_state(self.cfg)["projects"]["nexlink"])


class QuotaWaitTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.temp.cleanup)
        self.cfg = config.Config(state_dir=self.temp.name)
        self.project = rc.ProjectRelease(
            "nexlink", self.temp.name, target="develop", report="bureau",
            model="nexlink")

    def test_execute_skips_until_the_resume_instant_then_resumes(self):
        rc.save_state(self.cfg, {"schema": rc.SCHEMA, "projects": {"nexlink": {
            "quota_wait_until": 1260.0}}})
        engine = types.SimpleNamespace(
            execute=mock.Mock(return_value={"result": "waiting-ci"}))
        with mock.patch.object(rc, "release_provider", return_value=engine):
            report = rc.execute(self.cfg, None, self.project, now=1259.0)
            self.assertEqual(report["result"], "waiting-quota")
            self.assertEqual(report["resume_at"], 1260.0)
            engine.execute.assert_not_called()

            report = rc.execute(self.cfg, None, self.project, now=1260.0)
            self.assertEqual(report["result"], "waiting-ci")
            engine.execute.assert_called_once()

    def test_force_ignores_the_wait(self):
        rc.save_state(self.cfg, {"schema": rc.SCHEMA, "projects": {"nexlink": {
            "quota_wait_until": 1260.0}}})
        engine = types.SimpleNamespace(
            execute=mock.Mock(return_value={"result": "released"}))
        with mock.patch.object(rc, "release_provider", return_value=engine):
            report = rc.execute(self.cfg, None, self.project, now=1100.0,
                                force=True)
        self.assertEqual(report["result"], "released")
        engine.execute.assert_called_once()


class QuotaTickTest(unittest.TestCase):
    def _args(self):
        return types.SimpleNamespace(project="nexlink", now=100,
                                     release_command="tick", json=True)

    def _project(self):
        return rc.ProjectRelease("nexlink", "/unused", target="develop",
                                 model="nexlink")

    def test_waiting_quota_is_a_normal_tick_result(self):
        with mock.patch.object(release, "_project_names",
                               return_value=["nexlink"]), \
                mock.patch("ameesh.db.open_db"), \
                mock.patch.object(rc, "execute",
                                  return_value={"result": "waiting-quota"}), \
                mock.patch("ameesh.staging_pending.tick", return_value={}), \
                mock.patch("ameesh.staging_deferred.tick", return_value={}), \
                mock.patch("builtins.print"):
            self.assertEqual(
                release._release_tick(None, self._args(),
                                      {"nexlink": self._project()}),
                0)

    def test_the_first_quota_failure_schedules_instead_of_failing(self):
        with mock.patch.object(release, "_project_names",
                               return_value=["nexlink"]), \
                mock.patch("ameesh.db.open_db"), \
                mock.patch.object(rc, "execute",
                                  side_effect=rc.TransientReleaseError(QUOTA)), \
                mock.patch.object(release, "_report_error",
                                  return_value=True) as report, \
                mock.patch("builtins.print"):
            self.assertEqual(
                release._release_tick(None, self._args(),
                                      {"nexlink": self._project()}),
                0)
        report.assert_called_once()

    def test_a_non_quota_transient_failure_still_fails_the_tick(self):
        with mock.patch.object(release, "_project_names",
                               return_value=["nexlink"]), \
                mock.patch("ameesh.db.open_db"), \
                mock.patch.object(rc, "execute",
                                  side_effect=rc.TransientReleaseError(RIGHTS)), \
                mock.patch.object(release, "_report_error",
                                  return_value=False), \
                mock.patch("builtins.print"):
            self.assertEqual(
                release._release_tick(None, self._args(),
                                      {"nexlink": self._project()}),
                1)


if __name__ == "__main__":
    unittest.main()
