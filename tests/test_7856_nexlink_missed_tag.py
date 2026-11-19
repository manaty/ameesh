# SPDX-License-Identifier: AGPL-3.0-only
"""#7856: a published Nexlink tag is never reported as a missed cutoff.

The 19/11 10:00Z cutoff was tagged `release-11bt` at 10:46 by
ameesh-agents-1; the elected `mesh-design` role then moved to
ameesh-agents-2 at 10:49:49, whose local release state never recorded the
slot. The missed path did not read the durable tag and sent a false urgent
alert at 10:50Z. The tag is the durable fact; the local slot only caches it.

Review B (ameesh-cx2, #44156): the tag must name the cutoff's own execution,
not merely be put on any commit created in the window.
"""
from __future__ import annotations

import os
import tempfile
import types
import unittest
from unittest import mock

from ameesh import release_cutoff as rc
from ameesh.modules.nexlink import release as nx

SHA = "a" * 40
OLDER = "b" * 40
CUTOFF = 1000.0
LEGACY = "Nexlink cutoff: merge pinned main\n"


def listed(tags, stamps):
    return "".join("release-%s %s %s\n" % (version, commit,
                                           stamps.get(commit, ""))
                   for version, commit in tags.items())


def fake_git(listing, messages):
    def run(repo, *args, **kwargs):
        if args[:1] == ("show",):
            return types.SimpleNamespace(stdout=messages.get(args[-1], ""),
                                         returncode=0)
        return types.SimpleNamespace(stdout=listing, returncode=0)
    return run


class CutoffTagTest(unittest.TestCase):
    def setUp(self):
        self.project = rc.ProjectRelease("nexlink", "/unused", target="develop",
                                         github="acme/nexlink", model="nexlink")

    def read(self, stamps, tags, messages=None):
        messages = {SHA: LEGACY, OLDER: LEGACY} if messages is None else messages
        with mock.patch.object(nx, "release_tags", return_value=tags), \
                mock.patch.object(rc, "_git",
                                  side_effect=fake_git(listed(tags, stamps),
                                                       messages)):
            return nx.cutoff_tag(self.project, CUTOFF, 4600.0, window_s=1800.0)

    def test_the_legacy_candidate_created_in_the_window_names_the_cutoff(self):
        found = self.read({SHA: "1005", OLDER: "800"},
                          {"11bs": OLDER, "11bt": SHA})
        self.assertEqual(found["tag"], "release-11bt")
        self.assertEqual(found["commit"], SHA)

    def test_a_candidate_carrying_the_cutoff_marker_is_reconciled(self):
        message = "Nexlink cutoff 11bt: merge pinned main\n\ncutoff-at: 1000\n"
        found = self.read({SHA: "1005"}, {"11bt": SHA}, {SHA: message})
        self.assertEqual(found["tag"], "release-11bt")

    def test_a_manual_tag_on_an_ordinary_commit_is_not_the_cutoff(self):
        self.assertIsNone(self.read({SHA: "1005"}, {"11bt": SHA},
                                    {SHA: "manual release\n"}))

    def test_a_candidate_of_another_slot_is_not_this_cutoff(self):
        message = "Nexlink cutoff 11bt: merge pinned main\n\ncutoff-at: 5000\n"
        self.assertIsNone(self.read({SHA: "1005"}, {"11bt": SHA}, {SHA: message}))

    def test_a_candidate_named_for_another_version_is_not_this_cutoff(self):
        message = "Nexlink cutoff 11bs: merge pinned main\n\ncutoff-at: 1000\n"
        self.assertIsNone(self.read({SHA: "1005"}, {"11bt": SHA}, {SHA: message}))

    def test_a_tag_from_an_earlier_cutoff_does_not_reconcile_this_one(self):
        self.assertIsNone(self.read({OLDER: "800"}, {"11bs": OLDER}))

    def test_a_tag_created_after_the_window_does_not_reconcile_this_one(self):
        self.assertIsNone(self.read({SHA: "3000"}, {"11bt": SHA}))


class ReconciledCutoffTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(temp.cleanup)
        self.cfg = types.SimpleNamespace(state_dir=temp.name,
                                         host="ameesh-agents-2")
        self.project = rc.ProjectRelease("nexlink", "/unused", target="develop",
                                         github="acme/nexlink", model="nexlink")
        rc.save_state(self.cfg, {"schema": rc.SCHEMA, "projects": {"nexlink": {
            "last_error": "stale", "last_error_at": 900.0}}})
        for name, value in (
                ("compiled_rules", (object(), types.SimpleNamespace(commit="d" * 40))),
                ("cutoff_slot", (CUTOFF, "11:00")), ("fetch", None)):
            patcher = mock.patch.object(rc, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_tick(self, tags=None, stamps=None):
        tags = {"11bt": SHA} if tags is None else tags
        stamps = {SHA: "1005"} if stamps is None else stamps
        messages = {SHA: LEGACY}
        with mock.patch.object(nx, "release_tags", return_value=tags), \
                mock.patch.object(rc, "_git",
                                  side_effect=fake_git(listed(tags, stamps),
                                                       messages)), \
                mock.patch.object(rc, "send_report") as send:
            report = nx.execute(self.cfg, object(), self.project, now=4600.0)
        return report, send

    def test_a_published_tag_reconciles_without_a_missed_alarm(self):
        report, send = self.run_tick()
        self.assertEqual(report["result"], "reconciled")
        self.assertEqual(report["tag"], "release-11bt")
        self.assertEqual(report["release_sha"], SHA)
        self.assertEqual(report["host"], "ameesh-agents-2")
        send.assert_called_once()
        self.assertFalse(send.call_args.kwargs["urgent"])
        self.assertNotIn("MANQUÉE", send.call_args.args[3])
        slot = rc.load_state(self.cfg)["projects"]["nexlink"]
        self.assertEqual((slot["last_cutoff"], slot["last_result"],
                          slot["last_label"]), (CUTOFF, "reconciled", "11:00"))
        self.assertEqual(slot["last_release"]["tag"], "release-11bt")
        self.assertNotIn("last_error", slot)

    def test_without_a_tag_the_missed_alarm_still_goes_out(self):
        report, send = self.run_tick(tags={})
        self.assertEqual(report["result"], "missed")
        send.assert_called_once()
        self.assertTrue(send.call_args.kwargs["urgent"])
        self.assertIn("MANQUÉE", send.call_args.args[3])

    def test_a_tag_from_a_later_slot_does_not_silence_the_alarm(self):
        report, send = self.run_tick(tags={"11bu": SHA}, stamps={SHA: "5000"})
        self.assertEqual(report["result"], "missed")
        self.assertTrue(send.call_args.kwargs["urgent"])


if __name__ == "__main__":
    unittest.main()
