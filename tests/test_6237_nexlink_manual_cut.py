# SPDX-License-Identifier: AGPL-3.0-only
"""#6237: a manual Nexlink cutoff cuts its slot even after develop moved.

07/11: rel-01 tagged release-11aw on fb68c58db for the 22:00Z slot; develop
then moved to 3f6b7c618, so the base of the 23:28Z tick was not tagged and
#6152's exact-base proof missed it: the tick froze 11ax for the same slot.
The first tick of the hour before a cutoff records the published versions;
a version that appears afterwards was posted for that slot. Version order
alone proves nothing: an off-slot fix is newer too (review B/P2 #6237).
"""
import os
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from ameesh import nexlink_release as nx, release_cutoff as rc


HOUR = 3600.0
C1, C2 = 10 * HOUR, 22 * HOUR


class Rules:
    """Two cutoffs a day, at C1 and C2 (mod 24 h)."""

    def cutoff(self, now):
        day = (now // (24 * HOUR)) * 24 * HOUR
        for at in (day + C2, day + C1):
            if at <= now:
                return at
        return day - 24 * HOUR + C2


class Remote:
    """A remote whose published versions the test changes over time."""

    def __init__(self, *versions):
        self.versions = {value: "1" * 40 for value in versions}

    def tag(self, value):
        self.versions[value] = "2" * 40

    def __call__(self, project):
        return dict(self.versions)


class TickTests(unittest.TestCase):
    """Real ticks over a day: record, manual tag, develop moves, due tick."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.temp.cleanup)
        self.cfg = types.SimpleNamespace(state_dir=self.temp.name, host="h")
        self.project = rc.ProjectRelease("nexlink", "/unused", target="develop",
                                         github="acme/nexlink", model="nexlink")
        self.remote = Remote("11av")
        label = lambda rules, now: (Rules().cutoff(now), "x")
        for name, value in (("compiled_rules", (Rules(), types.SimpleNamespace(commit="d" * 40))),
                            ("fetch", None),
                            ("send_report", {}),
                            ("write_report", "/tmp/report.json")):
            patcher = mock.patch.object(rc, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for target, name, kwargs in (
                (rc, "cutoff_slot", dict(side_effect=label)),
                (rc, "due", dict(side_effect=self.due)),
                (nx, "release_tags", dict(side_effect=self.remote)),
                # develop moved past the manual tag: the tick's base is untagged
                (nx, "snapshot", dict(return_value=("b" * 40, "c" * 40, "a" * 40, {}, None))),
                (nx, "previous_release", dict(return_value="a" * 40)),
                (nx, "tagged_at", dict(return_value=set())),
                (nx, "drop_abandoned", dict(return_value=None)),
                (nx, "branches", dict(side_effect=self.remote)),
                (nx, "ensure_ref", dict(return_value=None)),
                (nx, "ci_plan", dict(return_value={"parts": [{"workflow": "w.yml"}],
                                                   "max_inflight": 2})),
                (nx, "workflow_step", dict(return_value=False)),
                (rc, "same_tree", dict(return_value=False))):
            patcher = mock.patch.object(target, name, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        merge = mock.patch.object(nx, "merge_candidate", return_value="e" * 40)
        self.merge = merge.start()
        self.addCleanup(merge.stop)

    @staticmethod
    def due(rules, now, slot, window):
        at = Rules().cutoff(now)
        if float(slot.get("last_cutoff") or 0) >= at or now - at > window:
            return None
        return {"missed": False}

    def tick(self, now):
        return nx.execute(self.cfg, object(), self.project, now=now)

    def slot(self):
        return rc.load_state(self.cfg)["projects"]["nexlink"]

    def test_manual_cut_for_the_slot_then_develop_moves_skips_it(self):
        """Witness: the real 11aw (22:00Z slot), then 3f6b7c618, then the tick."""
        self.tick(C2 - 50 * 60)          # the record of the 22:00 slot
        self.assertEqual(self.slot()["manual_watch"],
                         {"cutoff_at": C2, "versions": ["11av"]})
        self.remote.tag("11aw")          # rel-01, by hand, for this slot
        report = self.tick(C2 + 60)
        self.assertEqual(report["result"], "skipped")
        self.assertEqual(report["version"], "11aw")
        self.assertIn("déjà coupé à la main en release-11aw", report["message"])
        self.merge.assert_not_called()
        self.assertEqual(self.slot()["last_result"], "skipped")
        self.assertNotIn("pending_nexlink", self.slot())

    def test_manual_cut_of_the_previous_slot_does_not_skip_the_next(self):
        """B/P2: 10:00 slot recorded 11av; 11aw by hand, then develop moves."""
        self.tick(C1 - 30 * 60)
        self.remote.tag("11aw")          # manual cut for 10:00 (before its tick)
        self.assertEqual(self.tick(C1 + 60)["result"], "skipped")
        self.remote.tag("11ax")          # off-slot fix at 15:00
        self.tick(C2 - 40 * 60)          # the 22:00 record sees 11aw and 11ax
        report = self.tick(C2 + 60)
        self.assertEqual(report["result"], "waiting-ci")
        self.assertEqual(report["version"], "11ay")
        self.merge.assert_called_once()

    def test_without_a_record_the_slot_cuts(self):
        """A host that did not tick before the cutoff infers nothing."""
        self.remote.tag("11aw")
        report = self.tick(C2 + 60)
        self.assertEqual(report["version"], "11ax")
        self.merge.assert_called_once()

    def test_the_engine_publication_is_not_a_manual_cut(self):
        """A pending of the previous slot published inside the next record."""
        self.tick(C1 + 60)               # freezes 11aw for 10:00, CI pending
        self.tick(C2 - 30 * 60)          # record of 22:00 (11aw not yet tagged)
        with mock.patch.object(nx, "workflow_step", return_value=True), \
                mock.patch.object(nx, "migration_gate", return_value={}), \
                mock.patch.object(nx, "publish",
                                  side_effect=lambda project, pending:
                                  self.remote.tag(pending["version"])):
            self.assertEqual(self.tick(C2 - 20 * 60)["result"], "released")
        self.assertIn("11aw", self.slot()["manual_watch"]["versions"])
        report = self.tick(C2 + 60)
        self.assertEqual(report["result"], "waiting-ci")
        self.assertEqual(report["version"], "11ax")

    def test_the_record_is_taken_once_per_slot(self):
        self.tick(C2 - 50 * 60)
        self.remote.tag("11aw")
        self.tick(C2 - 10 * 60)
        self.assertEqual(self.slot()["manual_watch"]["versions"], ["11av"])

    def test_tagged_base_still_wins(self):
        """#6152 unchanged: the exact base names its own version."""
        self.tick(C2 - 50 * 60)
        self.remote.tag("11aw")
        with mock.patch.object(nx, "tagged_at", return_value={"11av"}):
            report = self.tick(C2 + 60)
        self.assertEqual(report["version"], "11av")
        self.assertIn("tête déjà coupée en release-11av", report["message"])


if __name__ == "__main__":
    unittest.main()
