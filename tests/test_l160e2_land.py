# SPDX-License-Identifier: AGPL-3.0-only
"""L160e2: ameesh merges the head of the queue — lease per target, merge in
the radar's clone with the prepared resolutions replayed, the project's
gates on the merged tree, push without force (`land: local`) or through the
PR (`land: pr`: merge pushed to the branch, CI, PR merged by the API), the
notice, the ejections, the alert `merge_blocked`, the settings.

Git repositories with a bare `origin`, storage mocked; the lease on real
Postgres."""
from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import time
import unittest
from unittest import mock

from ameesh import land, mail, merge_queue, radar, storage, work

from .support import PgTestCase
from .test_l160b_radar import MIG, _git
from .test_l160d_resolutions import RESOLVED, _manual_resolution
from .test_l160e_merge_queue import _Base


class FakeLocks:
    def __init__(self):
        self.held: dict = {}

    def land_lock(self, key, owner, ttl_s, *, epoch=None):
        now = time.time()
        cur = self.held.get(key)
        if epoch is not None:
            if cur and cur[0] == owner and cur[1] == epoch:
                self.held[key] = (owner, epoch, now + ttl_s)
                return epoch
            return None
        if cur and cur[0] not in (None, owner) and cur[2] > now:
            return None
        epoch = (cur[1] if cur else 0) + 1
        self.held[key] = (owner, epoch, now + ttl_s)
        return epoch

    def land_unlock(self, key, owner, epoch):
        cur = self.held.get(key)
        if cur and cur[0] == owner and cur[1] == epoch:
            self.held[key] = (None, epoch, 0)
            return True
        return False


class _Land(_Base):
    LAND = "local"

    def setUp(self):
        super().setUp()
        locks = FakeLocks()
        self.fake.land_lock = locks.land_lock
        self.fake.land_unlock = locks.land_unlock
        self.locks = locks
        self.origin = os.path.join(self.tmp, "origin.git")
        _git(self.tmp, "clone", "-q", "--bare", self.repo, self.origin)
        _git(self.repo, "remote", "add", "origin", self.origin)
        self.cfg = dataclasses.make_dataclass("Cfg", ["state_dir"])(self.tmp + "/state")
        self.project = radar.Project(name="p", repo=self.repo, migrations=MIG, land=self.LAND,
                                     gates=("test -f shared.py",), github="o/r")
        self.sent: list = []
        patch = mock.patch.object(mail, "send", side_effect=lambda db, s, to, body, **kw:
                                  self.sent.append((to, body, kw)) or len(self.sent))
        patch.start()
        self.addCleanup(patch.stop)

    def queued(self, lot):
        self.ready(lot)
        self.enter(lot)
        self.fake.queue_rows[lot]["rank"] = 1
        return self.fake._row(lot)

    def land(self, lot, **kw):
        return land.land_head(self.cfg, None, self.project, self.fake._row(lot),
                              owner="runner-1", **kw)

    def origin_log(self, ref="main"):
        return _git(self.origin, "log", "-1", "--format=%B", ref)


class LocalLandTest(_Land):
    def test_clean_lot_landed_and_noticed(self):
        self.queued(4)
        out = self.land(4)
        self.assertEqual(out["result"], "landed", out)
        self.assertIn("ameesh-work: 4", self.origin_log())
        self.assertEqual(_git(self.origin, "rev-parse", "main^2"), self.tip(4))
        self.assertEqual(self.fake.queue_rows[4]["state"], "landed")
        self.assertIsNone(self.locks.held["p:main"][0])  # lease freed
        (to, body, kw), = self.sent
        self.assertEqual((to, kw["payload"]["radar"]["kind"]), ("a4", "landed"))
        self.assertIn("`test -f shared.py`", body)
        # the agents' repository is untouched
        self.assertEqual(_git(self.repo, "status", "--porcelain"), "")

    def test_prepared_conflict_replayed_unprepared_ejected(self):
        _git(self.repo, "push", "-q", "origin", "lot/1-a:main")  # lot 1 already merged
        self.queued(2)
        out = self.land(2)
        self.assertEqual(out["result"], "ejected")
        self.assertIn("conflict on shared.py", self.fake.queue_rows[2]["reason"])
        self.assertIn("ameesh work queue add 2", self.sent[-1][1])
        sha = _manual_resolution(self.repo, "lot/2-b", "lot/1-a")
        self.fake.record_resolution(2, 1, commit=sha, lot_tip="x", other_tip="y", ref=None,
                                    files=["shared.py"], note="", actor="a2")
        self.queued(2)
        out = self.land(2)
        self.assertEqual(out["result"], "landed", out)
        self.assertEqual(_git(self.origin, "show", "main:shared.py"), RESOLVED.strip())
        self.assertIn("resolutions replayed by rerere on shared.py", self.sent[-1][1])

    def test_failed_gate_ejects_with_its_output(self):
        self.project = dataclasses.replace(self.project, gates=("echo boom; exit 3",))
        self.queued(4)
        before = _git(self.origin, "rev-parse", "main")
        out = self.land(4)
        self.assertEqual((out["result"], out["output"]), ("ejected", "boom"))
        self.assertEqual(_git(self.origin, "rev-parse", "main"), before)
        self.assertIn("gate `echo boom; exit 3` failed", self.fake.queue_rows[4]["reason"])
        self.assertIn("boom", self.sent[-1][1])

    def test_busy_lease(self):
        self.queued(4)
        self.locks.land_lock("p:main", "runner-2", 600)
        self.assertEqual(self.land(4)["result"], "busy")
        self.assertEqual(self.fake.queue_rows[4]["state"], "queued")

    def test_target_moved_during_the_merge_is_retried(self):
        self.queued(4)
        stale = _git(self.repo, "rev-parse", "main")
        _git(self.repo, "push", "-q", "origin", "lot/3-c:main")  # someone merged meanwhile
        with mock.patch.object(land, "fresh_target", return_value=stale):
            out = self.land(4)
        self.assertEqual(out["result"], "retry")
        self.assertEqual(self.fake.queue_rows[4]["state"], "queued")
        self.assertIn("retried", self.fake.queue_rows[4]["reason"])
        self.assertEqual(self.land(4)["result"], "landed")  # next pass, fresh target

    def test_run_pass_lands_the_head_only(self):
        self.queued(4)
        self.queued(3)
        self.fake.queue_rows[3]["rank"] = 2
        report = {"queue": {"targets": {"main": [{"lot": 4, "rank": 1, "state": "queued"},
                                                 {"lot": 3, "rank": 2, "state": "queued"}]}}}
        out = land.run_pass(self.cfg, None, self.project, report, owner="runner-1")
        self.assertEqual([(o["lot"], o["result"]) for o in out], [(4, "landed")])
        self.assertEqual(self.fake.queue_rows[3]["state"], "queued")
        off = dataclasses.replace(self.project, land="off")
        self.assertEqual(land.run_pass(self.cfg, None, off, report, owner="r"), [])


class FakeGh:
    def __init__(self):
        self.head = None
        self.ci = "pending"
        self.fail = False
        self.calls: list = []

    def __call__(self, args, timeout=60.0):
        self.calls.append(args)
        if self.fail:
            return subprocess.CompletedProcess(args, 4, "", "authentication required")
        if args[:2] == ["pr", "list"]:
            check = {"pending": {"status": "IN_PROGRESS", "conclusion": None},
                     "green": {"status": "COMPLETED", "conclusion": "SUCCESS"},
                     "red": {"status": "COMPLETED", "conclusion": "FAILURE"}}[self.ci]
            out = [{"number": 9, "headRefOid": self.head, "statusCheckRollup": [check]}]
            return subprocess.CompletedProcess(args, 0, json.dumps(out), "")
        return subprocess.CompletedProcess(args, 0, "", "")


class PrLandTest(_Land):
    LAND = "pr"

    def test_merge_pushed_to_the_branch_then_pr_merged_when_green(self):
        gh = FakeGh()
        self.queued(4)
        gh.head = self.tip(4)
        out = self.land(4, gh=gh)
        self.assertEqual(out["result"], "landing", out)
        row = self.fake.queue_rows[4]
        self.assertEqual(row["state"], "landing")
        self.assertEqual(_git(self.origin, "rev-parse", "lot/4-d"), out["sha"])
        self.assertIn("ameesh-work: 4", self.origin_log("lot/4-d"))
        # the radar sees the branch at the merge commit: still landing, not ejected
        lots = [dict(r) for r in self.fake.lots.values()]
        _git(self.repo, "fetch", "-q", "origin", "lot/4-d:lot/4-d")
        report = radar.scan(self.repo, lots, migrations=MIG)
        report["queue"] = merge_queue.run_pass(self.cfg, None, self.project, report)
        self.assertEqual(self.fake.queue_rows[4]["state"], "landing")
        self.assertEqual(report["queue"]["targets"]["main"][0]["state"], "landing")
        gh.head = out["sha"]
        self.assertEqual(self.land(4, gh=gh)["result"], "landing")  # CI pending
        gh.ci = "green"
        done = self.land(4, gh=gh)
        self.assertEqual(done["result"], "landed", done)
        merge_call = [c for c in gh.calls if c[:2] == ["pr", "merge"]][0]
        self.assertEqual(merge_call[-2:], ["--match-head-commit", out["sha"]])
        self.assertIn("--merge", merge_call)
        self.assertEqual(self.fake.queue_rows[4]["state"], "landed")

    def test_red_ci_or_moved_branch_ejects(self):
        for why in ("red", "moved"):
            with self.subTest(why):
                gh = FakeGh()
                self.queued(4)
                gh.head = self.tip(4)
                out = self.land(4, gh=gh)
                gh.head, gh.ci = (out["sha"], "red") if why == "red" else ("f" * 40, "green")
                self.assertEqual(self.land(4, gh=gh)["result"], "ejected")
                self.assertIn("red" if why == "red" else "moved",
                              self.fake.queue_rows[4]["reason"])
                self.assertFalse(any(c[:2] == ["pr", "merge"] for c in gh.calls))
                _git(self.repo, "push", "-q", "-f", "origin", "%s:lot/4-d" % self.tip(4))

    def test_pr_not_at_the_freeze_ejects_before_any_push(self):
        gh = FakeGh()
        self.queued(4)
        before = _git(self.origin, "rev-parse", "lot/4-d")
        gh.head, gh.ci = "e" * 40, "green"
        self.assertEqual(self.land(4, gh=gh)["result"], "ejected")
        self.assertIn("not at the freeze", self.fake.queue_rows[4]["reason"])
        self.assertEqual(_git(self.origin, "rev-parse", "lot/4-d"), before)

    def test_github_error_while_landing_keeps_it_landing(self):
        gh = FakeGh()
        self.queued(4)
        gh.head = self.tip(4)
        locks: dict = {}
        out = self.land(4, gh=gh, locks=locks)
        self.assertEqual(out["result"], "landing", out)
        gh.fail = True
        failed = self.land(4, gh=gh, locks=locks)
        self.assertEqual(failed["result"], "error", failed)
        self.assertIn("authentication required", failed["error"])
        row = self.fake.queue_rows[4]
        self.assertEqual((row["state"], row["landing_sha"]), ("landing", out["sha"]))
        self.assertTrue(locks, "the lease is kept")
        self.assertFalse(any(c[:2] == ["pr", "merge"] for c in gh.calls))
        gh.fail, gh.head, gh.ci = False, out["sha"], "green"
        self.assertEqual(self.land(4, gh=gh, locks=locks)["result"], "landed")

    def test_no_pr_ejects(self):
        self.queued(4)
        gh = mock.Mock(return_value=subprocess.CompletedProcess([], 0, "[]", ""))
        self.assertEqual(self.land(4, gh=gh)["result"], "ejected")
        self.assertIn("no open PR", self.fake.queue_rows[4]["reason"])


class SettingsAndAlertTest(unittest.TestCase):
    def test_settings(self):
        ok = radar.parse_settings({"projects": {"n": {"repo": "/r", "land": "local",
                                                      "gates": "make test"}}})
        self.assertEqual((ok.projects["n"].land, ok.projects["n"].gates,
                          ok.projects["n"].merge_method), ("local", ("make test",), "merge"))
        self.assertEqual(radar.parse_settings({"projects": {"n": "/r"}}).projects["n"].land,
                         "off")
        for bad, why in (({"land": "yes"}, "land"), ({"land": "pr"}, "gates"),
                         ({"merge_method": "ff"}, "merge_method"),
                         ({"land": "local", "gates": [""]}, "gates")):
            with self.subTest(why):
                with self.assertRaisesRegex(radar.SettingsError, why):
                    radar.parse_settings({"projects": {"n": dict(bad, repo="/r")}})

    def test_merge_blocked(self):
        now = time.time()
        rows = [{"work_item_id": 1, "target": "main", "state": "queued", "rank": 1,
                 "updated_ts": now - 3600, "assignee": "a1", "title": "t", "reason": "r"},
                {"work_item_id": 2, "target": "main", "state": "queued", "rank": 2,
                 "updated_ts": now - 3600},
                {"work_item_id": 3, "target": "dev", "state": "landing", "rank": 1,
                 "updated_ts": now - 60}]
        store = mock.Mock()
        store.work.queue.return_value = rows
        with mock.patch.object(storage, "of", return_value=store):
            from ameesh import exploitation
            alerts = exploitation._merge_blocked(None, now)
        self.assertEqual([(a["type"], a["lot"], a["agent"]) for a in alerts],
                         [("merge_blocked", 1, "a1")])
        self.assertIn("bloquée derrière le lot #1", alerts[0]["detail"])


class LeaseDbTest(PgTestCase):
    def test_lease(self):
        ops = storage.of(self.db).work
        one = ops.land_lock("p:main", "r1", 60)
        self.assertEqual(one, 1)
        self.assertIsNone(ops.land_lock("p:main", "r2", 60))
        self.assertEqual(ops.land_lock("p:main", "r1", 60, epoch=one), one)  # renewed
        self.assertIsNone(ops.land_lock("p:main", "r2", 60, epoch=one))
        # its own holder takes it again (restarted): new epoch, the old one fenced
        two = ops.land_lock("p:main", "r1", 60)
        self.assertEqual(two, 2)
        self.assertFalse(ops.land_unlock("p:main", "r1", one))
        self.assertFalse(ops.land_unlock("p:main", "r2", two))
        self.assertTrue(ops.land_unlock("p:main", "r1", two))
        self.assertEqual(ops.land_lock("p:main", "r2", 60), 3)
        # an expired lease is taken over, at a new epoch: the old holder is fenced
        self.db.execute("UPDATE merge_locks SET expires_at = now() - interval '1 second'")
        self.assertEqual(ops.land_lock("p:main", "r3", 60), 4)
        self.assertIsNone(ops.land_lock("p:main", "r2", 60, epoch=3))
        self.assertFalse(ops.land_unlock("p:main", "r2", 3))
        self.assertEqual(ops.land_lock("p:other", "r2", 60), 1)


if __name__ == "__main__":
    unittest.main()
