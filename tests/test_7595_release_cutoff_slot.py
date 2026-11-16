# SPDX-License-Identifier: AGPL-3.0-only
"""#7595: the Nexlink release slot names one cutoff, never moves backwards."""
import os
import tempfile
import types
import unittest
from unittest import mock

from ameesh import nexlink_release as nx, release_cutoff as rc

SHA = "a" * 40
BASE = "b" * 40
LATER = 200.0


def pending(**change):
    row = dict(
        schema=rc.SCHEMA, project="nexlink", version="11bo", tag="release-11bo",
        integration="integration/release-11bo", head="c" * 40, base=BASE,
        main="d" * 40, sha=SHA, cutoff_at=100.0, label="11:00", previous="e" * 40,
        empty=False, ci_max_inflight=3,
        ci_parts=[{"workflow": "backend-suite.yml"}],
        dispatches={"ci/backend-suite.yml": {
            "intent_at": 1.0, "accepted": True,
            "run": {"databaseId": 42, "status": "completed", "conclusion": "failure",
                    "url": "https://example/run/42"}}})
    row.update(change)
    return row


def later_slot():
    return {"last_cutoff": LATER, "last_result": "released", "last_label": "12:00"}


class SlotRecordingTests(unittest.TestCase):
    def test_an_older_cutoff_leaves_the_later_slot_intact(self):
        slot = later_slot()
        self.assertFalse(nx._record_cutoff(slot, 100.0, "11:00", "refused-ci"))
        self.assertEqual(slot, later_slot())

    def test_a_later_cutoff_records_its_own_result_and_label(self):
        slot = later_slot()
        self.assertTrue(nx._record_cutoff(slot, 300.0, "13:00", "refused-preflight"))
        self.assertEqual(slot, {"last_cutoff": 300.0,
                                "last_result": "refused-preflight",
                                "last_label": "13:00"})

    def test_the_same_cutoff_replaces_the_previous_result(self):
        slot = later_slot()
        self.assertTrue(nx._record_cutoff(slot, LATER, "12:00", "refused-ci"))
        self.assertEqual((slot["last_cutoff"], slot["last_result"], slot["last_label"]),
                         (LATER, "refused-ci", "12:00"))


class EngineSlotTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(temp.cleanup)
        self.cfg = types.SimpleNamespace(state_dir=temp.name, host="test-host")
        self.project = rc.ProjectRelease(
            "nexlink", "/unused", target="develop", github="acme/nexlink",
            model="nexlink", staging_handoff="bureau")

    def state(self, slot):
        rc.save_state(self.cfg, {"schema": rc.SCHEMA,
                                 "projects": {"nexlink": slot}})
        return rc.load_state(self.cfg)["projects"]["nexlink"]

    def test_an_older_abandon_keeps_the_later_slot_and_its_label(self):
        self.state(dict(later_slot(), pending_nexlink=pending()))

        def drop(project, row, save):
            row.pop("abandoned_integration", None)
            return "dropped"

        with mock.patch.object(nx, "release_tags", return_value={}), \
                mock.patch.object(nx, "drop_abandoned", side_effect=drop), \
                mock.patch.object(rc, "write_report", return_value="/state/report.json"), \
                mock.patch.object(rc, "send_report", return_value={}):
            nx.abandon(self.cfg, object(), self.project, now=300.0,
                       reason="red SQL invariant", actor="mesh-design")
        saved = rc.load_state(self.cfg)["projects"]["nexlink"]
        self.assertEqual((saved["last_cutoff"], saved["last_result"],
                          saved["last_label"]), (LATER, "released", "12:00"))
        trace = saved["abandoned_pending"]
        self.assertEqual((trace["cutoff_at"], trace["label"]), (100.0, "11:00"))
        self.assertNotIn("pending_nexlink", saved)

    def test_a_close_records_the_candidate_label(self):
        slot = self.state({"pending_nexlink": pending()})
        with mock.patch.object(rc, "write_report", return_value="/state/report.json"):
            nx._close(self.cfg, None, self.project, slot, pending(), "released",
                      lambda: None)
        self.assertEqual((slot["last_cutoff"], slot["last_result"], slot["last_label"]),
                         (100.0, "released", "11:00"))
        self.assertNotIn("pending_nexlink", slot)

    def test_an_older_close_leaves_the_later_slot_intact(self):
        slot = self.state(dict(later_slot(), pending_nexlink=pending()))
        with mock.patch.object(rc, "write_report", return_value="/state/report.json"):
            nx._close(self.cfg, None, self.project, slot, pending(), "released",
                      lambda: None)
        self.assertEqual((slot["last_cutoff"], slot["last_result"], slot["last_label"]),
                         (LATER, "released", "12:00"))
        self.assertNotIn("pending_nexlink", slot)

    def test_a_preflight_refusal_records_its_label(self):
        slot = self.state({"pending_nexlink": pending()})
        with mock.patch.object(nx, "drop_abandoned", return_value="absent"):
            with self.assertRaisesRegex(rc.ReleaseError, "bad workflow"):
                nx._preflight_refused(self.cfg, None, self.project, slot, pending(),
                                      rc.ReleaseError("bad workflow"),
                                      lambda: None, 300.0)
        self.assertEqual((slot["last_cutoff"], slot["last_result"], slot["last_label"]),
                         (100.0, "refused-preflight", "11:00"))
        self.assertNotIn("pending_nexlink", slot)

    def test_an_older_preflight_refusal_leaves_the_later_slot_intact(self):
        slot = self.state(dict(later_slot(), pending_nexlink=pending()))
        with mock.patch.object(nx, "drop_abandoned", return_value="absent"):
            with self.assertRaisesRegex(rc.ReleaseError, "bad workflow"):
                nx._preflight_refused(self.cfg, None, self.project, slot, pending(),
                                      rc.ReleaseError("bad workflow"),
                                      lambda: None, 300.0)
        self.assertEqual((slot["last_cutoff"], slot["last_result"], slot["last_label"]),
                         (LATER, "released", "12:00"))
        self.assertNotIn("pending_nexlink", slot)

    def test_an_older_skip_leaves_the_later_slot_intact(self):
        slot = self.state(dict(later_slot(), pending_nexlink=pending()))
        source = types.SimpleNamespace(commit="f" * 40)
        with mock.patch.object(rc, "write_report", return_value="/state/report.json"):
            nx._skip_cutoff(self.cfg, None, self.project, slot, 100.0, "11:00",
                            source, 300.0, lambda: None, version="11bo",
                            head="c" * 40, base=BASE)
        self.assertEqual((slot["last_cutoff"], slot["last_result"], slot["last_label"]),
                         (LATER, "released", "12:00"))
        self.assertNotIn("pending_nexlink", slot)

    def test_a_missed_cutoff_records_its_label(self):
        slot = self.state({"pending_nexlink": pending()})
        source = types.SimpleNamespace(commit="f" * 40)
        nx._missed_cutoff(self.cfg, None, self.project, slot, 100.0, "11:00",
                          source, 300.0, lambda: None)
        self.assertEqual((slot["last_cutoff"], slot["last_result"], slot["last_label"]),
                         (100.0, "missed", "11:00"))


if __name__ == "__main__":
    unittest.main()
