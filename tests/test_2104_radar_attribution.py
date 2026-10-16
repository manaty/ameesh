# SPDX-License-Identifier: AGPL-3.0-only
"""Real Git witnesses for inherited, native, mixed and rename conflicts."""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from unittest import mock

from ameesh import merge_queue, radar, radar_attribution, resolutions
from .test_l160b_radar import _git, _write, git_env


TEXT = "".join("line %d\n" % n for n in range(30))


def edit(line, value):
    return TEXT.replace("line %d\n" % line, value + "\n")


class AttributionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ameesh-2104-")
        self.addCleanup(self.tmp.cleanup)
        self.repo = self.tmp.name
        _git(self.repo, "init", "-q", "-b", "main")
        for path in ("inherited.py", "shared.py", "old.py"):
            _write(self.repo, path, TEXT)
        self.commit("base")
        self.base = _git(self.repo, "rev-parse", "HEAD")
        self.lots = [{"id": 1, "branch": "lot/a", "state": "qa", "priority": 1},
                     {"id": 2, "branch": "lot/b", "state": "build", "priority": 2}]

    def commit(self, message):
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", message)

    def branch(self, name, base, edits):
        _git(self.repo, "checkout", "-qb", name, base)
        for path, text in edits.items():
            _write(self.repo, path, text)
        self.commit(name)

    def fixture(self, *, native=False, same_file=False):
        self.branch("lot/a", self.base,
                    {"inherited.py": edit(0, "old lot"),
                     "shared.py": edit(0, "a")})
        _git(self.repo, "checkout", "-q", "main")
        _write(self.repo, "inherited.py", edit(0, "target"))
        self.commit("target moves")
        edits = {"shared.py": edit(0 if native else 29, "b")}
        if same_file:
            edits["inherited.py"] = edit(0, "target").replace("line 29\n", "b\n")
        self.branch("lot/b", "main", edits)
        _git(self.repo, "checkout", "-q", "main")

    def scan(self, **kwargs):
        return radar.scan(self.repo, self.lots, default_target="main", **kwargs)

    def annotate(self, report, rows=(), replayer=None):
        resolutions.annotate(report, list(rows), {r["id"]: r for r in self.lots},
                             replayer=replayer)

    def test_inherited_outside_partner_delta_is_not_assigned(self):
        self.fixture()
        before = _git(self.repo, "show-ref")
        objects = _git(self.repo, "count-objects", "-v")
        report = self.scan()
        pair = report["pairs"][0]
        self.assertEqual(pair["state"], "target-conflict")
        self.assertEqual(pair["inherited_files"], ["inherited.py"])
        self.assertEqual(pair["files"], [])
        self.assertEqual(pair["uncertain_files"], [])
        self.assertEqual(pair["shared"], ["shared.py"])
        self.annotate(report)
        self.assertNotIn("resolver", pair)
        self.assertEqual(set(radar.conflicts_of(report)), {"target:1"})
        self.assertEqual(before, _git(self.repo, "show-ref"))
        self.assertEqual(objects, _git(self.repo, "count-objects", "-v"))
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".git", "rr-cache")))

    def test_native_conflict_keeps_resolver(self):
        self.branch("lot/a", self.base, {"shared.py": edit(0, "a")})
        self.branch("lot/b", self.base, {"shared.py": edit(0, "b")})
        report = self.scan()
        pair = report["pairs"][0]
        self.assertEqual(pair["state"], "conflict")
        self.assertEqual(pair["files"], ["shared.py"])
        self.assertFalse(pair["attribution_pending"])
        self.annotate(report)
        self.assertEqual(pair["resolver"]["lot"], 2)

    def test_mixed_conflict_keeps_native_evidence_without_assigning_inherited(self):
        self.fixture(native=True)
        report = self.scan()
        pair = report["pairs"][0]
        self.assertEqual(pair["state"], "conflict")
        self.assertEqual(pair["files"], ["shared.py"])
        self.assertEqual(pair["inherited_files"], ["inherited.py"])
        self.annotate(report)
        self.assertNotIn("resolver", pair)
        conflict = radar.conflicts_of(report)["pair:1-2"]
        text = radar.message_text(conflict, 2, {r["work_item"]: r for r in report["branches"]})
        self.assertIn("No resolver is assigned", text)
        self.assertNotIn("ameesh work resolve", text)

    def test_same_filename_is_not_proof_of_inheritance(self):
        self.fixture(same_file=True)
        report = self.scan()
        pair = report["pairs"][0]
        self.assertEqual(pair["state"], "unclassified")
        self.assertEqual(pair["uncertain_files"], ["inherited.py"])
        self.assertEqual(pair["inherited_files"], [])
        self.annotate(report)
        self.assertNotIn("resolver", pair)
        self.assertIn("unclassified: inherited.py", "\n".join(radar.render(report)))
        self.assertEqual(radar.conflicts_of(report)["pair:1-2"]["files"], ["inherited.py"])

    def test_rename_rename_keeps_entire_conflict_group(self):
        self.branch("lot/a", self.base, {"shared.py": edit(0, "a")})
        _git(self.repo, "mv", "old.py", "a.py")
        self.commit("rename a")
        self.branch("lot/b", self.base, {"shared.py": edit(29, "b")})
        _git(self.repo, "mv", "old.py", "b.py")
        self.commit("rename b")
        report = self.scan()
        pair = report["pairs"][0]
        self.assertEqual(pair["state"], "conflict")
        self.assertEqual(pair["files"], ["a.py", "b.py", "old.py"])
        self.assertEqual(pair["shared"], ["old.py", "shared.py"])
        self.annotate(report)
        self.assertIn("resolver", pair)

    def test_probe_failure_does_not_claim_clean_or_assign(self):
        self.fixture(native=True)
        for result in (None, {"groups": [], "stages": {}}):
            with self.subTest(result=result):
                with mock.patch.object(radar_attribution, "read_merge", return_value=result):
                    report = self.scan()
                pair = report["pairs"][0]
                self.assertEqual(pair["state"], "unclassified")
                self.assertEqual(pair["uncertain_files"], ["inherited.py", "shared.py"])
                self.annotate(report)
                self.assertNotIn("resolver", pair)

    def test_inherited_rename_is_proven_as_one_group(self):
        self.branch("lot/a", self.base, {"shared.py": edit(0, "a")})
        _git(self.repo, "mv", "old.py", "a.py")
        self.commit("old lot rename")
        _git(self.repo, "checkout", "-q", "main")
        _git(self.repo, "mv", "old.py", "target.py")
        self.commit("target rename")
        self.branch("lot/b", "main", {"shared.py": edit(29, "b")})
        pair = self.scan()["pairs"][0]
        self.assertEqual(pair["state"], "target-conflict")
        self.assertEqual(pair["inherited_files"], ["a.py", "old.py", "target.py"])
        self.assertEqual(pair["uncertain_files"], [])

    def test_attribution_does_not_depend_on_lot_order(self):
        self.fixture(native=True)
        first = self.scan()["pairs"][0]
        self.lots.reverse()
        reverse = self.scan()["pairs"][0]
        for key in ("state", "files", "inherited_files", "uncertain_files"):
            self.assertEqual(first[key], reverse[key])

    def test_queue_keeps_uncertainty_unprepared_without_assigning_it(self):
        self.fixture(same_file=True)
        report = self.scan()
        entries = [{"work_item_id": n, "priority": 2, "queued_ts": 1} for n in (1, 2)]
        ranked = merge_queue.order(entries, merge_queue.pair_states(report))
        self.assertEqual(ranked[1]["waits"], [(1, None)])

    def test_stale_mixed_preparation_does_not_request_inherited_resolution(self):
        self.fixture(native=True)
        report = self.scan()
        row = {"work_item_id": 2, "other_item_id": 1, "commit_sha": "a" * 40}
        replay = mock.Mock()
        replay.prime.return_value = []
        replay.replay.return_value = {"state": "stale", "remaining": ["inherited.py"]}
        self.annotate(report, [row], replay)
        pair = report["pairs"][0]
        self.assertEqual(pair["resolution"]["replay"], "stale")
        self.assertEqual(pair["state"], "conflict")
        self.assertNotIn("stale:1-2", radar.conflicts_of(report))

    def test_cache_reuses_witnesses_and_changes_when_target_moves(self):
        self.fixture()
        cache = radar.Cache()
        self.scan(cache=cache)
        with mock.patch.object(radar_attribution, "classify", side_effect=AssertionError("reprobe")):
            self.scan(cache=cache)
        _write(self.repo, "unrelated.py", "new target\n")
        self.commit("new target")
        with mock.patch.object(radar_attribution, "classify", wraps=radar_attribution.classify) as probe:
            self.scan(cache=cache)
        self.assertEqual(probe.call_count, 1)

    def test_existing_preparation_stays_visible_without_reassigning(self):
        self.fixture()
        report = self.scan()
        row = {"work_item_id": 2, "other_item_id": 1, "commit_sha": "a" * 40}
        replay = mock.Mock()
        replay.prime.return_value = []
        replay.replay.return_value = {"state": "prepared", "remaining": []}
        self.annotate(report, [row], replay)
        pair = report["pairs"][0]
        self.assertEqual(pair["state"], "target-conflict")
        self.assertEqual(pair["resolution"]["replay"], "prepared")
        self.assertNotIn("resolver", pair)
        replay.replay.assert_called_once()

    def test_old_divergence_already_resolved_on_target_is_superseded(self):
        self.branch("history/a", self.base, {"inherited.py": edit(0, "a history")})
        self.branch("history/b", self.base, {"inherited.py": edit(0, "b history")})
        _git(self.repo, "checkout", "-q", "main")
        _git(self.repo, "merge", "--ff-only", "history/a")
        result = subprocess.run(["git", "-C", self.repo, "merge", "--no-edit", "history/b"],
                                env=git_env(), capture_output=True)
        self.assertEqual(result.returncode, 1)
        _write(self.repo, "inherited.py", edit(0, "resolved on target"))
        self.commit("target resolves history")
        self.branch("lot/a", "history/a", {"shared.py": edit(0, "a")})
        self.branch("lot/b", "history/b", {"shared.py": edit(29, "b")})
        report = self.scan()
        pair = report["pairs"][0]
        self.assertEqual(pair["state"], "superseded")
        self.assertEqual(pair["inherited_files"], ["inherited.py"])
        self.assertTrue(all(item["state"] == "clean" for item in report["against_target"]))
        self.annotate(report)
        self.assertNotIn("resolver", pair)
        self.assertEqual(radar.conflicts_of(report), {})


if __name__ == "__main__":
    unittest.main()
