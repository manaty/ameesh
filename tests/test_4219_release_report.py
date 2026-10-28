# SPDX-License-Identifier: AGPL-3.0-only
"""#4219: a skipped staging is not a successful one, and the cutoff report
reaches the project's pilot, not only a human whose mail no agent reads.

The 1.27.0 cutoff (28/10/2026, 10:00Z) ran on agents-2 with `--no-staging`:
its report said `staging_ok: true`, was not urgent, and went to `smichea`
only, where it sat undelivered. No database, no Git, no host.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ameesh import config, release_cutoff


class FinishTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = config.Config(host="ameesh-agents-2", humans="smichea",
                                 state_dir=self.tmp.name)
        self.project = release_cutoff.ProjectRelease(
            name="ameesh", repo=self.tmp.name, target="main", report="smichea")

    def finish(self, staging, no_staging, db=None):
        report = {"schema": release_cutoff.SCHEMA, "result": "released",
                  "project": "ameesh", "version": "1.27.0", "tag": "v1.27.0",
                  "release_sha": "03dab6e829e679dc63d8eb4e2c52bdeae6c0f3d7",
                  "cutoff_at": 1793181600.0, "label": "11:00", "staging": staging,
                  "deviations": []}
        return release_cutoff.finish(self.cfg, db, self.project, report, {}, {},
                                     1793181600.0, "11:00", None, None, None,
                                     staging, no_staging)

    def test_no_staging_is_not_a_successful_staging(self):
        report = self.finish(None, no_staging=True)
        self.assertFalse(report["staging_ok"])
        self.assertEqual(report["staging_skipped"], "--no-staging")
        written = json.loads(Path(report["report_path"]).read_text())
        self.assertFalse(written["staging_ok"])

    def test_staging_run_with_exit_zero_stays_ok(self):
        report = self.finish({"exit": 0, "duration_s": 1.0, "log": "j"}, no_staging=False)
        self.assertTrue(report["staging_ok"])
        self.assertNotIn("staging_skipped", report)

    def test_no_staging_report_is_urgent_and_says_so(self):
        sent = []
        with mock.patch.object(release_cutoff, "_report_body", return_value="corps"), \
                mock.patch.object(release_cutoff, "send_report",
                                  side_effect=lambda *a, **k: sent.append((a, k))):
            self.finish(None, no_staging=True, db=object())
        (args, kwargs), = sent
        self.assertTrue(kwargs["urgent"])
        self.assertIn("non lancé (--no-staging)", args[3])
        self.assertIn("03dab6e829e6", args[3])


class RecipientsTest(unittest.TestCase):

    def project(self, report):
        return release_cutoff.ProjectRelease(name="ameesh", repo="/unused",
                                             target="main", report=report)

    def recipients(self, report, pilots=None, error=None):
        overview = mock.patch("ameesh.registry.overview", return_value=[])
        if error is not None:
            pilot = mock.patch("ameesh.alert_briefs.project_pilots", side_effect=error)
        else:
            pilot = mock.patch("ameesh.alert_briefs.project_pilots",
                               return_value=list(pilots or []))
        with overview, pilot:
            return release_cutoff.report_recipients(object(), object(),
                                                    self.project(report))

    def test_human_recipient_gets_the_pilot_in_copy(self):
        self.assertEqual(self.recipients("smichea", ["mesh-design"]),
                         ["smichea", "mesh-design"])

    def test_pilot_recipient_is_not_doubled(self):
        self.assertEqual(self.recipients("mesh-design", ["mesh-design"]), ["mesh-design"])

    def test_unreadable_role_keeps_the_configured_recipient(self):
        self.assertEqual(self.recipients("smichea", error=RuntimeError("canon")),
                         ["smichea"])

    def test_send_report_mails_each_recipient_once(self):
        with mock.patch.object(release_cutoff, "report_recipients",
                               return_value=["smichea", "mesh-design"]), \
                mock.patch.object(release_cutoff.mail, "send") as send:
            release_cutoff.send_report(object(), object(), self.project("smichea"),
                                       "corps", {"tag": "v1.27.0"}, urgent=True)
        self.assertEqual([call.args[2] for call in send.call_args_list],
                         ["smichea", "mesh-design"])
        self.assertTrue(all(call.kwargs["payload"]["urgent"]
                            for call in send.call_args_list))


if __name__ == "__main__":
    unittest.main()
