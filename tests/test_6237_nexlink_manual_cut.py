# SPDX-License-Identifier: AGPL-3.0-only
"""#6237: a manual Nexlink cutoff cuts its slot even after develop moved.

07/11: rel-01 tagged release-11aw on fb68c58db for the 22:00Z slot; develop
then moved to 3f6b7c618, so the base of the 23:28Z tick was not tagged and
#6152's exact-base proof missed it: the tick froze 11ax for the same slot.
A version newer than the last one the state recorded is that manual cutoff.
"""
import os
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from ameesh import nexlink_release as nx, release_cutoff as rc


class CutSinceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.temp.cleanup)
        self.remote = Path(self.temp.name) / "remote.git"
        subprocess.check_output(["git", "init", "-q", "--bare", str(self.remote)])
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "develop")
        self.git("config", "user.name", "test")
        self.git("config", "user.email", "test@localhost")
        self.git("remote", "add", "origin", str(self.remote))
        self.project = rc.ProjectRelease("nexlink", str(self.repo), target="develop",
                                         model="nexlink")

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], text=True,
                                       stderr=subprocess.STDOUT).strip()

    def commit(self, name):
        (self.repo / name).write_text(name)
        self.git("add", ".")
        self.git("commit", "-qm", name)
        return self.git("rev-parse", "HEAD")

    def tag(self, version, sha):
        self.git("tag", "release-" + version, sha)
        self.git("push", "-q", "origin", "refs/tags/release-" + version)

    def test_manual_tag_then_develop_moves_is_a_cut(self):
        """Witness: manual cut, develop moves on, the tick's base is untagged."""
        self.tag("11av", self.commit("previous"))
        manual = self.commit("fb68c58db")
        self.tag("11aw", manual)
        moved = self.commit("3f6b7c618")
        self.git("push", "-q", "origin", "develop")
        self.assertIsNone(nx.already_cut(self.project, moved, manual))
        self.assertEqual(nx.cut_since(self.project, "11av"), "11aw")

    def test_nothing_newer_than_the_record_is_not_a_cut(self):
        self.tag("11av", self.commit("previous"))
        self.commit("fresh")
        self.assertIsNone(nx.cut_since(self.project, "11av"))
        # A frozen version abandoned before its tag (reused next time).
        self.assertIsNone(nx.cut_since(self.project, "11aw"))

    def test_no_record_infers_nothing(self):
        self.tag("11aw", self.commit("manual"))
        self.assertIsNone(nx.cut_since(self.project, None))
        self.assertIsNone(nx.cut_since(self.project, "not a version"))


class ExecuteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.temp.cleanup)
        self.cfg = types.SimpleNamespace(state_dir=self.temp.name, host="h")
        self.project = rc.ProjectRelease("nexlink", "/unused", target="develop",
                                         github="acme/nexlink", model="nexlink")
        for name, value in (("compiled_rules", (object(), types.SimpleNamespace(commit="d" * 40))),
                            ("cutoff_slot", (1000, "23:00")),
                            ("due", {"missed": False}),
                            ("fetch", None),
                            ("send_report", {}),
                            ("write_report", "/tmp/report.json")):
            patcher = mock.patch.object(rc, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name, value in (("snapshot", ("b" * 40, "c" * 40, "a" * 40, {}, None)),
                            ("previous_release", "a" * 40),
                            ("tagged_at", set()),
                            ("drop_abandoned", None)):
            patcher = mock.patch.object(nx, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def save_slot(self, **slot):
        rc.save_state(self.cfg, {"schema": rc.SCHEMA, "projects": {"nexlink": slot}})

    def slot(self):
        return rc.load_state(self.cfg)["projects"]["nexlink"]

    def cut(self, known):
        with mock.patch.object(nx, "branches", return_value=known), \
                mock.patch.object(nx, "merge_candidate", return_value="e" * 40) as merge, \
                mock.patch.object(nx, "ensure_ref") as ensure, \
                mock.patch.object(nx, "ci_plan",
                                  return_value={"parts": [{"workflow": "backend-suite.yml"}],
                                                "max_inflight": 2}), \
                mock.patch.object(nx, "workflow_step", return_value=False), \
                mock.patch.object(nx, "publish") as publish, \
                mock.patch.object(rc, "same_tree", return_value=False):
            report = nx.execute(self.cfg, object(), self.project, now=1000)
        return report, merge, ensure, publish

    def test_manual_cut_since_the_last_slot_skips_the_slot(self):
        """The real 11ax: 11av recorded, 11aw tagged by hand on an older commit."""
        self.save_slot(last_cutoff=500, last_result="released", release_seen="11av")
        report, merge, ensure, publish = self.cut({"11av": "1" * 40, "11aw": "2" * 40})
        self.assertEqual(report["result"], "skipped")
        self.assertEqual(report["version"], "11aw")
        self.assertEqual(report["tag"], "release-11aw")
        self.assertIn("déjà coupé à la main en release-11aw", report["message"])
        merge.assert_not_called()
        ensure.assert_not_called()
        publish.assert_not_called()
        slot = self.slot()
        self.assertEqual(slot["last_result"], "skipped")
        self.assertEqual(slot["last_cutoff"], 1000)
        self.assertEqual(slot["release_seen"], "11aw")
        self.assertNotIn("pending_nexlink", slot)
        self.assertFalse(rc.send_report.call_args.kwargs["urgent"])

    def test_next_slot_after_the_manual_cut_cuts_normally(self):
        self.save_slot(last_cutoff=500, last_result="skipped", release_seen="11aw")
        report, merge, _, _ = self.cut({"11av": "1" * 40, "11aw": "2" * 40})
        self.assertEqual(report["result"], "waiting-ci")
        self.assertEqual(report["version"], "11ax")
        merge.assert_called_once()
        self.assertEqual(self.slot()["release_seen"], "11ax")

    def test_without_record_the_slot_cuts_and_records(self):
        self.save_slot(last_cutoff=500, last_result="released")
        report, merge, _, _ = self.cut({"11aw": "2" * 40})
        self.assertEqual(report["version"], "11ax")
        merge.assert_called_once()
        self.assertEqual(self.slot()["release_seen"], "11ax")

    def test_tagged_base_still_wins_over_the_record(self):
        """#6152 unchanged: the exact base names its own version."""
        self.save_slot(last_cutoff=500, last_result="released", release_seen="11av")
        with mock.patch.object(nx, "tagged_at", return_value={"11aw"}):
            report, merge, _, _ = self.cut({"11av": "1" * 40, "11aw": "c" * 40})
        self.assertEqual(report["version"], "11aw")
        self.assertIn("tête déjà coupée en release-11aw", report["message"])
        merge.assert_not_called()

    def test_missed_slot_forgets_the_record(self):
        """A manual remedy tag for a missed slot is not the next slot's cut."""
        self.save_slot(last_cutoff=500, last_result="released", release_seen="11av")
        with mock.patch.object(rc, "due", return_value={"missed": True}):
            report = nx.execute(self.cfg, None, self.project, now=1000)
        self.assertEqual(report["result"], "missed")
        self.assertNotIn("release_seen", self.slot())

    def test_close_records_the_published_version(self):
        slot = {"pending_nexlink": {}, "release_seen": "11av"}
        pending = {"version": "11aw", "cutoff_at": 1000, "pr": None}
        with mock.patch.object(nx, "_announce"):
            nx._close(self.cfg, None, self.project, slot, pending, "released", lambda: None)
        self.assertEqual(slot["release_seen"], "11aw")
        nx._see(slot, "11av")
        self.assertEqual(slot["release_seen"], "11aw")


if __name__ == "__main__":
    unittest.main()
