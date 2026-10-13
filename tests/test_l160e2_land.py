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

from ameesh import land, mail, merge_queue, radar, resolutions, review_lot, storage, work

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

    def holds(self, lease):
        key, owner, epoch = lease
        cur = self.held.get(key)
        return bool(cur and cur[0] == owner and cur[1] == epoch and cur[2] > time.time())

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
        self.fake.lease_ok = locks.holds
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

    def enter(self, lot, ci=None):
        """Admission fetches origin into the radar's clone (L160e1)."""
        clone = resolutions.Replayer(self.repo, radar.clone_dir(self.cfg, "p"))
        return merge_queue.enter(None, lot, repo=self.repo, actor="orch", ci=ci, clone=clone)

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


class ReviewAfterMergeTest(LocalLandTest):
    """L539 : la fusion confirmée d'un lot léger ouvre le lot de relecture §2."""

    def test_light_merge_opens_the_review_lot(self):
        self.queued(4)
        calls = []

        def opened(db, **kw):
            calls.append(kw)
            return {"lot": kw["lot"], "id": 900, "created": True, "class": "light",
                    "why": "ouvert"}

        with mock.patch.object(merge_queue, "_class_of",
                               return_value={"class": "light", "why": "policy of ameesh"}), \
                mock.patch.object(review_lot, "open_after_merge", side_effect=opened):
            out = self.land(4)
        self.assertEqual(out["result"], "landed", out)
        self.assertEqual(out["review_lot"]["id"], 900)
        (kw,) = calls
        self.assertEqual(kw["lot"], 4)
        self.assertEqual(kw["merged_sha"], _git(self.origin, "rev-parse", "main"))
        self.assertEqual(kw["target"], "main")

    def test_a_refused_landing_opens_nothing(self):
        self.project = dataclasses.replace(self.project, gates=("exit 3",))
        self.queued(4)
        with mock.patch.object(review_lot, "open_after_merge") as opened:
            out = self.land(4)
        self.assertEqual(out["result"], "ejected", out)
        opened.assert_not_called()

    def test_a_doubt_or_a_higher_class_opens_nothing(self):
        """La classe relue n'est pas `light` : rien n'est ouvert."""
        self.queued(4)
        with mock.patch.object(merge_queue, "_class_of",
                               return_value={"class": "sensitive", "why": "doute"}):
            out = self.land(4)
        self.assertEqual(out["result"], "landed", out)
        self.assertFalse(out["review_lot"]["created"], out)
        self.assertIn("sensitive", out["review_lot"]["why"])


class FakeGh:
    def __init__(self):
        self.head = None
        self.base = "main"
        self.ci = "pending"
        self.fail = False
        self.merge_result = (0, "", "")
        self.base_on_merge = None
        self.calls: list = []

    def __call__(self, args, timeout=60.0):
        self.calls.append(args)
        if self.fail:
            return subprocess.CompletedProcess(args, 4, "", "authentication required")
        if args[:2] == ["pr", "list"]:
            check = {"pending": {"status": "IN_PROGRESS", "conclusion": None},
                     "green": {"status": "COMPLETED", "conclusion": "SUCCESS"},
                     "red": {"status": "COMPLETED", "conclusion": "FAILURE"}}[self.ci]
            out = [{"number": 9, "headRefOid": self.head, "baseRefName": self.base,
                    "statusCheckRollup": [check]}]
            return subprocess.CompletedProcess(args, 0, json.dumps(out), "")
        if args[:2] == ["pr", "merge"]:
            if self.merge_result[0] == 0 and self.base_on_merge:
                self.base = self.base_on_merge      # retargeted just before the merge
            return subprocess.CompletedProcess(args, *self.merge_result)
        if args[:2] == ["pr", "view"]:
            out = {"baseRefName": self.base, "headRefOid": self.head, "state": "OPEN"}
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

    def test_the_already_merged_path_opens_the_review_lot(self):
        """L539 : le chemin « déjà fusionné » (PR disparue, lot fusionné) ouvre
        lui aussi le lot de relecture, avec le sha du jalon `merged`."""
        gh = FakeGh()
        self.queued(4)
        gh.head = self.tip(4)
        locks: dict = {}
        self.assertEqual(self.land(4, gh=gh, locks=locks)["result"], "landing")
        self.fake.lots[4]["state"] = "merged"
        self.marks[4] = [{"id": 3, "kind": "merged", "sha": "d" * 40, "at_ts": 3.0}]
        calls = []

        def no_pr(args, timeout=60.0):
            if args[:2] == ["pr", "list"]:
                return subprocess.CompletedProcess(args, 0, "[]", "")
            return subprocess.CompletedProcess(args, 0, "", "")

        with mock.patch.object(review_lot, "open_after_merge",
                               side_effect=lambda db, **kw: calls.append(kw)
                               or {"id": 901, "created": True}):
            out = self.land(4, gh=no_pr, locks=locks)
        self.assertEqual(out["result"], "landed", out)
        self.assertEqual(out["review_lot"]["id"], 901)
        self.assertEqual(calls[0]["merged_sha"], "d" * 40)


def _steal_after_gates(test, key="p:main"):
    """Gates that outlive the lease: it expires, runner-2 takes it (epoch 2)."""
    real = land.gates

    def gates(project, path, **kw):
        out = real(project, path, **kw)
        owner, epoch, _ = test.locks.held[key]
        test.locks.held[key] = (owner, epoch, 0)          # expired
        test.assertIsNotNone(test.locks.land_lock(key, "runner-2", 600))
        return out
    return mock.patch.object(land, "gates", side_effect=gates)


class Review194Test(_Land):
    """#194 r3: the two P1 and the two P2."""

    def test_lease_lost_during_the_gates_nothing_pushed_local(self):
        self.queued(4)
        before = _git(self.origin, "rev-parse", "main")
        with _steal_after_gates(self):
            out = self.land(4)
        self.assertEqual(out["result"], "busy", out)
        self.assertEqual(_git(self.origin, "rev-parse", "main"), before)
        self.assertNotEqual(self.fake.queue_rows[4]["state"], "landed")
        self.assertEqual(self.locks.held["p:main"][0], "runner-2")
        # runner-2 lands it from the start (a `landing` lot, in local mode)
        out = land.land_head(self.cfg, None, self.project, self.fake._row(4),
                             owner="runner-2")
        self.assertEqual(out["result"], "landed", out)

    def test_lease_lost_during_the_gates_nothing_pushed_pr(self):
        self.project = dataclasses.replace(self.project, land="pr")
        gh = FakeGh()
        self.queued(4)
        gh.head = self.tip(4)
        before = _git(self.origin, "rev-parse", "lot/4-d")
        with _steal_after_gates(self):
            out = self.land(4, gh=gh)
        self.assertEqual(out["result"], "busy", out)
        self.assertEqual(_git(self.origin, "rev-parse", "lot/4-d"), before)

    def test_pr_on_another_base_is_ejected_before_any_push(self):
        self.project = dataclasses.replace(self.project, land="pr")
        gh = FakeGh()
        self.queued(4)
        gh.head, gh.base, gh.ci = self.tip(4), "develop", "green"
        before = _git(self.origin, "rev-parse", "lot/4-d")
        self.assertEqual(self.land(4, gh=gh)["result"], "ejected")
        self.assertIn("targets develop, not main", self.fake.queue_rows[4]["reason"])
        self.assertEqual(_git(self.origin, "rev-parse", "lot/4-d"), before)
        self.assertFalse(any(c[:2] == ["pr", "merge"] for c in gh.calls))

    def test_pr_retargeted_while_landing_is_ejected(self):
        self.project = dataclasses.replace(self.project, land="pr")
        gh = FakeGh()
        self.queued(4)
        gh.head = self.tip(4)
        out = self.land(4, gh=gh)
        gh.head, gh.base, gh.ci = out["sha"], "develop", "green"
        self.assertEqual(self.land(4, gh=gh)["result"], "ejected")
        self.assertFalse(any(c[:2] == ["pr", "merge"] for c in gh.calls))

    def _landing(self):
        self.project = dataclasses.replace(self.project, land="pr")
        gh = FakeGh()
        self.queued(4)
        gh.head = self.tip(4)
        locks: dict = {}
        out = self.land(4, gh=gh, locks=locks)
        gh.head, gh.ci = out["sha"], "green"
        return gh, locks, out["sha"]

    def test_transient_merge_refusal_keeps_it_landing(self):
        gh, locks, sha = self._landing()
        gh.merge_result = (1, "", "GraphQL: Base branch was modified")
        out = self.land(4, gh=gh, locks=locks)
        self.assertEqual(out["result"], "retry", out)
        row = self.fake.queue_rows[4]
        self.assertEqual((row["state"], row["landing_sha"]), ("landing", sha))
        self.assertTrue(locks, "the lease is kept")
        gh.merge_result = (0, "", "")
        self.assertEqual(self.land(4, gh=gh, locks=locks)["result"], "landed")

    def test_merge_failure_without_output_is_a_failure(self):
        gh, locks, sha = self._landing()
        gh.merge_result = (1, "", "")
        out = self.land(4, gh=gh, locks=locks)
        self.assertEqual((out["result"], out["error"]), ("retry", "exit 1"))
        self.assertEqual(self.fake.queue_rows[4]["state"], "landing")
        self.assertEqual(land.pr_merge("o/r", 9, sha, "merge",
                                       gh=lambda a, timeout=0: subprocess.CompletedProcess(
                                           a, 1, "", "")), "exit 1")


class StaleExecutorTest(_Land):
    """#194 r4: a pause after the renewal, beyond the lease: another executor
    takes the next epoch and lands; the first one's push is refused."""

    def pause_after_renewal(self, then, nth=1):
        """The `nth` renewal of runner-1 (the one right before the push or
        the merge) succeeds, then the lease expires, runner-2 takes it and
        `then()` runs (what runner-2 does)."""
        real = self.locks.land_lock
        seen = []

        def lock(key, owner, ttl_s, *, epoch=None):
            got = real(key, owner, ttl_s, epoch=epoch)
            if epoch is not None and owner == "runner-1" and got is not None:
                seen.append(epoch)
            if len(seen) == nth and epoch is not None and owner == "runner-1" \
                    and got is not None:
                seen.append(None)
                held = self.locks.held[key]
                self.locks.held[key] = (held[0], held[1], 0)
                self.assertIsNotNone(real(key, "runner-2", 600))
                then()
            return got
        self.fake.land_lock = lock

    def test_local_push_refused_never_landed(self):
        self.queued(4)
        moved = {}

        def runner2():
            # runner-2, under the next epoch, lands another lot first (the same
            # lot would give the very same merge commit within a second, and
            # the push of an identical commit is rightly a no-op)
            self.fake.land_lock = self.locks.land_lock
            _git(self.repo, "push", "-q", "origin", "lot/3-c:main")
            moved["sha"] = _git(self.origin, "rev-parse", "main")
        self.pause_after_renewal(runner2)
        out = self.land(4)
        self.assertEqual(out["result"], "busy", out)
        self.assertEqual(_git(self.origin, "rev-parse", "main"), moved["sha"])
        self.assertNotIn("ameesh-work: 4", self.origin_log())
        self.assertNotEqual(self.fake.queue_rows[4]["state"], "landed")

    def test_local_next_epoch_sees_it_already_landed(self):
        self.queued(4)
        _git(self.repo, "push", "-q", "origin", "lot/4-d:main")   # pushed by an earlier epoch
        before = _git(self.origin, "rev-parse", "main")
        out = self.land(4)
        self.assertEqual(out["result"], "landed", out)
        self.assertEqual(_git(self.origin, "rev-parse", "main"), before)  # nothing pushed

    def test_pr_target_moved_since_the_merge_is_not_merged(self):
        self.project = dataclasses.replace(self.project, land="pr")
        gh = FakeGh()
        self.queued(4)
        gh.head = self.tip(4)
        out = self.land(4, gh=gh)
        self.assertEqual(out["result"], "landing", out)
        _git(self.repo, "push", "-q", "origin", "lot/3-c:main")  # someone moved main
        gh.head, gh.ci = out["sha"], "green"
        self.assertEqual(self.land(4, gh=gh)["result"], "ejected")
        self.assertIn("since the merge", self.fake.queue_rows[4]["reason"])
        self.assertFalse(any(c[:2] == ["pr", "merge"] for c in gh.calls))

    def test_pr_push_refused_not_ejected(self):
        self.project = dataclasses.replace(self.project, land="pr")
        gh = FakeGh()
        self.queued(4)
        gh.head = self.tip(4)
        moved = {}

        def runner2():
            # runner-2 pushed its own merge commit to the branch meanwhile
            _git(self.repo, "push", "-q", "-f", "origin", "lot/3-c:lot/4-d")
            moved["sha"] = _git(self.origin, "rev-parse", "lot/4-d")
            self.fake.queue_rows[4]["landing_sha"] = moved["sha"]
        self.pause_after_renewal(runner2)
        out = self.land(4, gh=gh)
        self.assertEqual(out["result"], "busy", out)
        self.assertEqual(_git(self.origin, "rev-parse", "lot/4-d"), moved["sha"])
        self.assertNotEqual(self.fake.queue_rows[4]["state"], "ejected")
        self.assertEqual(self.fake.queue_rows[4]["landing_sha"], moved["sha"])


class EpochFenceTest(_Land):
    """#194 r5: a stale holder that would succeed is fenced by the epoch —
    on the remote (local mode, lease ref) and in every queue write."""

    pause_after_renewal = StaleExecutorTest.pause_after_renewal

    def acquire_runner2(self, lease_ref=True):
        """runner-2 takes the next epoch and, in local mode, moves the remote
        lease ref — WITHOUT moving the target (its gates still run)."""
        key = "p:main"
        epoch = self.locks.held[key][1]
        if lease_ref:
            clone = resolutions.Replayer(self.repo, radar.clone_dir(self.cfg, "p")).path
            land.take_remote_lease(clone, self.origin, land.LEASE_REF + "main", key, epoch,
                                   "runner-2")

    def test_local_stale_push_refused_target_unmoved(self):
        self.queued(4)
        before = _git(self.origin, "rev-parse", "main")
        self.pause_after_renewal(self.acquire_runner2)
        out = self.land(4)
        self.assertEqual(out["result"], "busy", out)
        self.assertEqual(_git(self.origin, "rev-parse", "main"), before)
        self.assertNotEqual(self.fake.queue_rows[4]["state"], "landed")

    def test_pr_stale_writes_fail(self):
        self.project = dataclasses.replace(self.project, land="pr")
        gh = FakeGh()
        self.queued(4)
        gh.head = self.tip(4)
        self.pause_after_renewal(lambda: self.acquire_runner2(lease_ref=False))
        out = self.land(4, gh=gh)
        self.assertEqual(out["result"], "busy", out)
        self.assertIsNone(self.fake.queue_rows[4]["landing_sha"])

    def test_stale_follower_never_overwrites_landed(self):
        self.project = dataclasses.replace(self.project, land="pr")
        gh = FakeGh()
        self.queued(4)
        gh.head = self.tip(4)
        locks: dict = {}
        out = self.land(4, gh=gh, locks=locks)
        gh.head, gh.ci = out["sha"], "green"

        def runner2_lands():
            self.acquire_runner2(lease_ref=False)
            self.fake.queue_rows[4]["state"] = "landed"           # runner-2 merged it
            _git(self.repo, "push", "-q", "origin", "lot/3-c:main")
        # renewal 1: taking the lease again; 2: right before the merge
        self.pause_after_renewal(runner2_lands, nth=2)
        stale = self.land(4, gh=gh, locks=locks)
        self.assertEqual(stale["result"], "busy", stale)
        self.assertEqual(self.fake.queue_rows[4]["state"], "landed")
        self.assertFalse(any(c[:2] == ["pr", "merge"] for c in gh.calls))

    def test_pr_retargeted_after_the_status_read(self):
        self.project = dataclasses.replace(self.project, land="pr")
        gh = FakeGh()
        self.queued(4)
        gh.head = self.tip(4)
        locks: dict = {}
        out = self.land(4, gh=gh, locks=locks)
        gh.head, gh.ci = out["sha"], "green"
        real = gh.__call__

        def retarget(args, timeout=60.0):
            if args[:2] == ["pr", "view"]:
                gh.base = "develop"                    # between the list and the merge
            return real(args, timeout=timeout)
        stale = self.land(4, gh=retarget, locks=locks)
        self.assertEqual(stale["result"], "ejected", stale)
        self.assertIn("changed right before its merge", self.fake.queue_rows[4]["reason"])
        self.assertFalse(any(c[:2] == ["pr", "merge"] for c in gh.calls))

    def test_pr_retargeted_inside_the_last_window_is_reported(self):
        self.project = dataclasses.replace(self.project, land="pr")
        gh = FakeGh()
        self.queued(4)
        gh.head = self.tip(4)
        locks: dict = {}
        out = self.land(4, gh=gh, locks=locks)
        gh.head, gh.ci, gh.base_on_merge = out["sha"], "green", "develop"
        done = self.land(4, gh=gh, locks=locks)
        self.assertEqual((done["result"], done["wrong_base"]), ("landed", "develop"))
        self.assertTrue(self.fake.queue_rows[4]["reason"].startswith(land.WRONG_BASE))
        self.assertTrue(any("merged into develop, not main" in body for _, body, _ in self.sent))

    def test_lease_ref_as_a_branch_prefix(self):
        settings = radar.parse_settings({"projects": {"p": {
            "repo": self.repo, "land": "local", "gates": ["true"],
            "lease_ref": "ameesh-land/"}}})
        self.assertEqual(settings.projects["p"].lease_ref, "refs/heads/ameesh-land/")
        for bad in ("ameesh-land", "refs/ameesh/../x/", 3):
            with self.assertRaises(radar.SettingsError):
                radar.parse_settings({"projects": {"p": {
                    "repo": self.repo, "land": "local", "gates": ["true"], "lease_ref": bad}}})
        self.project = dataclasses.replace(self.project, lease_ref="refs/heads/ameesh-land/")
        self.queued(4)
        out = self.land(4)
        self.assertEqual(out["result"], "landed", out)
        self.assertTrue(_git(self.origin, "rev-parse", "refs/heads/ameesh-land/main"))

    def test_lease_ref_not_moved_nothing_written(self):
        self.queued(4)
        hook = os.path.join(self.origin, "hooks", "pre-receive")
        with open(hook, "w") as fh:
            fh.write("#!/bin/sh\ngrep -q refs/ameesh/ && exit 1\nexit 0\n")
        os.chmod(hook, 0o755)
        before = _git(self.origin, "rev-parse", "main")
        notes, reason = len(self.fake.notes), self.fake.queue_rows[4]["reason"]
        out = self.land(4)
        self.assertEqual(out["result"], "error", out)
        self.assertIn("lease ref", out["error"])
        # #194 r6: nothing written at all
        self.assertEqual(len(self.fake.notes), notes)
        self.assertEqual(self.fake.queue_rows[4]["reason"], reason)
        self.assertIn("radar.projects.p.lease_ref", out["error"])
        self.assertEqual(_git(self.origin, "rev-parse", "main"), before)
        self.assertEqual(self.fake.queue_rows[4]["state"], "queued")
        self.assertIsNone(self.locks.held["p:main"][0])        # lease released


class RemoteLockTest(_Land):
    """#194 r6: the remote lease ref is the lock; the landing push changes it,
    so the server checks it; the queue writes lock the lease row."""

    pause_after_renewal = StaleExecutorTest.pause_after_renewal

    def clone(self):
        return resolutions.Replayer(self.repo, radar.clone_dir(self.cfg, "p")).path

    def test_lease_ref_moved_by_the_server_during_the_push(self):
        # what runner-2 does between the advertisement and runner-1's update:
        # a pre-receive hook takes the remote lease for a newer epoch
        self.queued(4)
        mark = os.path.join(self.tmp, "steal")
        hook = os.path.join(self.origin, "hooks", "pre-receive")
        with open(hook, "w") as fh:
            fh.write("#!/bin/sh\ncat >/dev/null\nif [ -f %s ]; then rm -f %s\n"
                     "export GIT_AUTHOR_NAME=r2 GIT_AUTHOR_EMAIL=r2@x GIT_COMMITTER_NAME=r2"
                     " GIT_COMMITTER_EMAIL=r2@x\n"
                     "t=$(git mktree </dev/null)\n"
                     "c=$(git commit-tree $t -m 'ameesh land lease p:main epoch 99 (runner-2)')\n"
                     "env -u GIT_QUARANTINE_PATH git update-ref %smain $c\nfi\nexit 0\n"
                     % (mark, mark, land.LEASE_REF))
        os.chmod(hook, 0o755)
        before = _git(self.origin, "rev-parse", "main")

        def arm():
            open(mark, "w").close()
        self.pause_after_renewal(lambda: (arm(), self.locks.land_lock("p:main", "runner-2", 600)))
        out = self.land(4)
        self.assertNotEqual(out["result"], "landed", out)
        self.assertEqual(_git(self.origin, "rev-parse", "main"), before)
        self.assertIn("epoch 99", _git(self.origin, "log", "-1", "--format=%s",
                                       land.LEASE_REF + "main"))

    def test_a_late_stale_publisher_never_rolls_the_epoch_back(self):
        clone = self.clone()
        os.makedirs(os.path.dirname(clone), exist_ok=True)
        resolutions.Replayer(self.repo, clone).ensure()
        ref = land.LEASE_REF + "main"
        land.take_remote_lease(clone, self.origin, ref, "p:main", 2, "runner-2")
        two = _git(self.origin, "rev-parse", ref)
        with self.assertRaises(land.LandError):
            land.take_remote_lease(clone, self.origin, ref, "p:main", 1, "runner-1")
        self.assertEqual(_git(self.origin, "rev-parse", ref), two)

    def test_first_to_the_server_wins_the_next_epoch_agrees(self):
        # runner-2 takes the database lease but has not published yet: the
        # remote lock is still runner-1's, whose landing is then the valid one
        # — and (#194 r8) reported as such, recorded under its freeze
        self.queued(4)
        self.pause_after_renewal(lambda: self.locks.land_lock("p:main", "runner-2", 600))
        out = self.land(4)
        self.assertEqual(out["result"], "landed", out)
        self.assertIn("ameesh-work: 4", self.origin_log())
        self.assertEqual(self.fake.queue_rows[4]["state"], "landed")

    def test_unreadable_base_after_the_merge_is_uncertain(self):
        self.project = dataclasses.replace(self.project, land="pr")
        gh = FakeGh()
        self.queued(4)
        gh.head = self.tip(4)
        locks: dict = {}
        out = self.land(4, gh=gh, locks=locks)
        gh.head, gh.ci = out["sha"], "green"
        real = gh.__call__
        merged = []

        def flaky(args, timeout=60.0):
            if args[:2] == ["pr", "merge"]:
                merged.append(True)
            elif args[:2] == ["pr", "view"] and merged:
                return subprocess.CompletedProcess(args, 1, "", "HTTP 502")
            return real(args, timeout=timeout)
        done = self.land(4, gh=flaky, locks=locks)
        self.assertEqual(done["result"], "uncertain", done)
        self.assertTrue(self.fake.queue_rows[4]["reason"].startswith(land.UNCERTAIN))
        self.assertTrue(any("base unreadable after the merge" in body
                            for _, body, _ in self.sent))
        store = mock.Mock()
        store.work.queue.return_value = [dict(self.fake.queue_rows[4], assignee="a4",
                                              updated_ts=1.0)]
        store.work.events_noted.return_value = []
        with mock.patch.object(storage, "of", return_value=store):
            from ameesh import exploitation, notify
            alerts = exploitation._land_uncertain(None)
        self.assertEqual([(a["type"], a["lot"]) for a in alerts], [("land_uncertain", 4)])
        self.assertIn("land_uncertain", notify.URGENT_TYPES)

    def test_wrong_base_warned_even_with_the_lease_lost(self):
        self.project = dataclasses.replace(self.project, land="pr")
        gh = FakeGh()
        self.queued(4)
        gh.head = self.tip(4)
        locks: dict = {}
        out = self.land(4, gh=gh, locks=locks)
        gh.head, gh.ci, gh.base_on_merge = out["sha"], "green", "develop"
        real = gh.__call__

        def expire_on_merge(args, timeout=60.0):
            if args[:2] == ["pr", "merge"]:
                held = self.locks.held["p:main"]
                self.locks.held["p:main"] = (held[0], held[1], 0)
                self.locks.land_lock("p:main", "runner-2", 600)
            return real(args, timeout=timeout)
        done = self.land(4, gh=expire_on_merge, locks=locks)
        self.assertEqual(done["wrong_base"], "develop", done)
        self.assertTrue(any("merged into develop, not main" in body for _, body, _ in self.sent))
        self.assertTrue(self.fake.queue_rows[4]["reason"].startswith(land.WRONG_BASE))


class Review194r8Test(_Land):
    """#194 r7 verdict: a stale doubt never overwrites a newer freeze; a lease
    ref never collides with a target; a merge that happened is reported as
    one (malformed read-back, lease lost, timeout); the alerts reach a human
    assignee."""
    LAND = "pr"

    def landing(self):
        gh = FakeGh()
        self.queued(4)
        gh.head = self.tip(4)
        locks: dict = {}
        out = self.land(4, gh=gh, locks=locks)
        gh.head, gh.ci = out["sha"], "green"
        return gh, locks, out["sha"]

    def lose_lease(self):
        held = self.locks.held["p:main"]
        self.locks.held["p:main"] = (held[0], held[1], 0)
        self.locks.land_lock("p:main", "runner-2", 600)

    def test_stale_doubt_never_overwrites_a_new_freeze(self):
        gh, locks, sha = self.landing()
        merged = []

        def refrozen_meanwhile(args, timeout=60.0):
            if args[:2] == ["pr", "merge"]:
                merged.append(True)
                # runner-1 suspended: runner-2 refreezes and requeues the lot
                self.lose_lease()
                self.fake.queue_rows[4].update(state="queued", frozen_sha="b" * 40,
                                               landing_sha=None, reason="")
            elif args[:2] == ["pr", "view"] and merged:
                return subprocess.CompletedProcess(args, 1, "", "HTTP 502")
            return gh(args, timeout=timeout)
        done = self.land(4, gh=refrozen_meanwhile, locks=locks)
        self.assertEqual((done["result"], done["recorded"]), ("uncertain", "history"), done)
        row = self.fake.queue_rows[4]
        self.assertEqual((row["state"], row["frozen_sha"], row["reason"]),
                         ("queued", "b" * 40, ""))
        self.assertTrue(any(n.startswith(land.UNCERTAIN) for _, n, _ in self.fake.notes))
        self.assertEqual([r["work_item_id"] for r in land.uncertain(None)], [4])

    def test_lease_ref_colliding_with_a_target_refused(self):
        def settings(lease_ref, target="main"):
            return radar.parse_settings({"projects": {"p": {
                "repo": self.repo, "land": "local", "gates": ["true"], "target": target,
                "lease_ref": lease_ref}}})
        for bad, target in (("refs/heads/", "main"), ("refs/tags/x/", "main"),
                            ("ameesh-land/", "ameesh-land/main"), ("ameesh-land/", "ameesh-land"),
                            ("refs/heads/feature/", "main")):
            with self.subTest(bad=bad, target=target):
                with self.assertRaises(radar.SettingsError):
                    settings(bad, target)
        self.assertEqual(settings("refs/ameesh/land/").projects["p"].lease_ref,
                         "refs/ameesh/land/")
        self.assertEqual(settings("ameesh-land/").projects["p"].lease_ref,
                         "refs/heads/ameesh-land/")
        # declared without the settings: refused at landing, nothing written
        self.project = dataclasses.replace(self.project, land="local", lease_ref="refs/heads/")
        self.queued(4)
        before = _git(self.origin, "rev-parse", "main")
        out = self.land(4)
        self.assertEqual(out["result"], "error", out)
        self.assertIn("collides with the target main", out["error"])
        self.assertEqual(_git(self.origin, "rev-parse", "main"), before)
        self.assertEqual(self.fake.queue_rows[4]["state"], "queued")

    def test_malformed_read_back_after_the_merge_is_uncertain(self):
        gh, locks, sha = self.landing()
        merged = []

        def garbled(args, timeout=60.0):
            if args[:2] == ["pr", "merge"]:
                merged.append(True)
            elif args[:2] == ["pr", "view"] and merged:
                return subprocess.CompletedProcess(args, 0, "<html>502</html>", "")
            return gh(args, timeout=timeout)
        done = self.land(4, gh=garbled, locks=locks)
        self.assertEqual(done["result"], "uncertain", done)
        self.assertTrue(self.fake.queue_rows[4]["reason"].startswith(land.UNCERTAIN))
        self.assertEqual(self.fake.queue_rows[4]["state"], "landed")

    def test_lease_lost_after_a_successful_merge_is_still_landed(self):
        gh, locks, sha = self.landing()

        def expire_on_merge(args, timeout=60.0):
            if args[:2] == ["pr", "merge"]:
                self.lose_lease()
            return gh(args, timeout=timeout)
        done = self.land(4, gh=expire_on_merge, locks=locks)
        self.assertEqual(done["result"], "landed", done)
        self.assertEqual(self.fake.queue_rows[4]["state"], "landed")

    def merging(self, gh, *, merges: bool, readable: bool):
        """`pr merge` times out; GitHub merged it (`merges`) or not."""
        state = {"merged": False, "called": False}

        def timeout(args, timeout=60.0):
            if args[:2] == ["pr", "merge"]:
                state.update(merged=merges, called=True)
                return subprocess.CompletedProcess(args, land.GH_TIMEOUT, "",
                                                   "timed out after 120 seconds")
            if args[:2] == ["pr", "view"]:
                if not readable and state["called"]:
                    return subprocess.CompletedProcess(args, 1, "", "HTTP 502")
                out = {"baseRefName": gh.base, "headRefOid": gh.head,
                       "state": "MERGED" if state["merged"] else "OPEN"}
                return subprocess.CompletedProcess(args, 0, json.dumps(out), "")
            if args[:2] == ["pr", "list"] and state["merged"]:
                rows = [{"number": 9, "headRefOid": gh.head, "baseRefName": gh.base}] \
                    if "merged" in args else []
                return subprocess.CompletedProcess(args, 0, json.dumps(rows), "")
            return gh(args, timeout=timeout)
        return timeout, state

    def test_merge_timeout_read_back_merged(self):
        gh, locks, sha = self.landing()
        fake, _ = self.merging(gh, merges=True, readable=True)
        done = self.land(4, gh=fake, locks=locks)
        self.assertEqual(done["result"], "landed", done)
        self.assertEqual(self.fake.queue_rows[4]["state"], "landed")

    def test_merge_timeout_unreadable_is_uncertain_then_found_merged(self):
        gh, locks, sha = self.landing()
        fake, state = self.merging(gh, merges=True, readable=False)
        done = self.land(4, gh=fake, locks=locks)
        self.assertEqual(done["result"], "uncertain", done)
        row = self.fake.queue_rows[4]
        self.assertEqual(row["state"], "landing")
        self.assertTrue(row["reason"].startswith(land.UNCERTAIN))
        self.assertTrue(locks, "the lease is kept")
        # next pass: the PR is no longer open, found merged at the landing commit
        fake, state = self.merging(gh, merges=True, readable=True)
        state["merged"] = True
        again = self.land(4, gh=lambda a, timeout=60.0: fake(a, timeout) if a[:2] != [
            "pr", "merge"] else self.fail("merged twice"), locks=locks)
        self.assertEqual(again["result"], "landed", again)
        self.assertEqual(self.fake.queue_rows[4]["state"], "landed")

    def test_merge_timeout_open_remains_uncertain(self):
        gh, locks, sha = self.landing()
        fake, state = self.merging(gh, merges=False, readable=True)
        done = self.land(4, gh=fake, locks=locks)
        self.assertEqual(done["result"], "uncertain", done)
        self.assertEqual(self.fake.queue_rows[4]["state"], "landing")
        self.assertTrue(self.fake.queue_rows[4]["reason"].startswith(land.UNCERTAIN))
        self.assertIn("state OPEN", done["reason"])
        self.assertIn("complete later", self.sent[-1][1])
        self.assertTrue(locks)
        state["merged"] = True
        again = self.land(4, gh=lambda args, timeout=60: fake(args, timeout)
                          if args[:2] != ["pr", "merge"] else self.fail("merged twice"), locks=locks)
        self.assertEqual(again["result"], "landed", again)

    def test_valid_lease_never_marks_a_replacement_entry_landed(self):
        for change in ("generation", "frozen_id", "target", "removed-and-reentered"):
            with self.subTest(change=change):
                _git(self.repo, "push", "-q", "-f", "origin", "lot/4-d")
                gh, locks, sha = self.landing()
                replacement = {}

                def replace_entry(args, timeout=60):
                    if args[:2] == ["pr", "merge"]:
                        row = self.fake.queue_rows[4]
                        if change == "removed-and-reentered":
                            old = dict(row)
                            self.fake.queue_remove(4, note="removed", actor="r2")
                            self.fake.queue_add(4, target=old["target"], frozen_sha=old["frozen_sha"],
                                                frozen_id=old["frozen_id"], note="reentered", actor="r2")
                            row = self.fake.queue_rows[4]
                            row.update(state="landing", landing_sha=sha)
                        elif change == "target":
                            row["target"] = "release"
                        else:
                            row[change] += 1
                        replacement.update(row)
                        self.assertTrue(self.locks.holds(("p:main", "runner-1", locks["p:main"])))
                    return gh(args, timeout=timeout)
                done = self.land(4, gh=replace_entry, locks=locks)
                self.assertEqual(done["result"], "landed", done)
                self.assertEqual(self.fake.queue_rows[4], replacement)
                self.assertIn("history", done.get("warning", ""))
                self.assertIn("history:", self.fake.notes[-1][1])

    def test_post_merge_recording_error_retries_without_resetting_queue(self):
        for persistent in (False, True):
            with self.subTest(persistent=persistent):
                _git(self.repo, "push", "-q", "-f", "origin", "lot/4-d")
                gh, locks, sha = self.landing()
                update = self.fake.queue_update
                attempts = []
                notice_before_record = []

                def flaky(item, **kw):
                    if kw.get("state") == "landed":
                        attempts.append(True)
                        notice_before_record.append(any("merged by ameesh" in body for _, body, _ in self.sent))
                        if persistent or len(attempts) == 1:
                            raise OSError("transient recording failure")
                    return update(item, **kw)
                with mock.patch.object(self.fake, "queue_update", side_effect=flaky):
                    done = self.land(4, gh=gh, locks=locks)
                self.assertEqual(done["result"], "landed", done)
                self.assertEqual(len(attempts), 2)
                self.assertEqual(notice_before_record, [True, True])
                self.assertEqual(self.fake.queue_rows[4]["state"], "landing" if persistent else "landed")
                if persistent:
                    self.assertIn("recording failed", done["warning"])
                    self.assertIn("could not be recorded", self.sent[-1][1])

    def test_cross_project_lease_namespace_collision_is_rejected(self):
        raw = {"projects": {
            "p": {"repo": self.repo, "target": "main", "land": "local",
                  "gates": ["true"], "lease_ref": "ameesh-land/"},
            "q": {"repo": self.repo, "target": "ameesh-land/main"}}}
        with self.assertRaisesRegex(radar.SettingsError, "collides.*ameesh-land/main"):
            radar.parse_settings(raw)
        # Bypass settings validation: landing itself still refuses the write.
        project = dataclasses.replace(self.project, land="local", lease_ref="refs/heads/ameesh-land/")
        other = radar.Project(name="q", repo=self.repo + "-q", target="ameesh-land/main")
        self.project = project
        self.queued(4)
        _git(self.origin, "branch", "ameesh-land/main", "main")
        before = _git(self.origin, "rev-parse", "ameesh-land/main")
        with mock.patch.object(radar, "settings_of", return_value=radar.Settings(projects={"p": project, "q": other})):
            done = self.land(4)
        self.assertEqual(done["result"], "error", done)
        self.assertIn("collides with the target ameesh-land/main", done["error"])
        self.assertEqual(_git(self.origin, "rev-parse", "ameesh-land/main"), before)
        # A per-lot target absent from settings is protected too.
        self.queued(3)
        self.fake.queue_rows[3]["target"] = "ameesh-land/main"
        done = self.land(4)
        self.assertEqual(done["result"], "error", done)
        self.assertEqual(_git(self.origin, "rev-parse", "ameesh-land/main"), before)

    def test_history_only_alert_keeps_the_human_responsible(self):
        from ameesh import exploitation, notify
        self.fake.lots[4]["assignee"] = "human:alice"
        gh, locks, sha = self.landing()

        def removed(args, timeout=60):
            if args[:2] == ["pr", "merge"]:
                self.fake.queue_remove(4, note="removed", actor="r2")
                gh.base = "release"
            return gh(args, timeout=timeout)
        done = self.land(4, gh=removed, locks=locks)
        self.assertEqual(done["recorded"], "history", done)
        alert, = exploitation._land_uncertain(None)
        self.assertEqual((alert.get("agent"), alert.get("responsible")), (None, "human:alice"))
        router = notify.Router.__new__(notify.Router)
        self.assertEqual(router.resolve(alert), ("human:alice", "alerte"))

    def test_alerts_reach_a_human_assignee(self):
        from ameesh import exploitation, notify
        store = mock.Mock()
        store.work.queue.return_value = [{
            "work_item_id": 4, "target": "main", "state": "landed", "rank": None,
            "reason": land.UNCERTAIN + "x", "updated_ts": 1.0, "assignee": "human:alice"}]
        store.work.events_noted.return_value = []
        with mock.patch.object(storage, "of", return_value=store):
            alert = exploitation._land_uncertain(None)[0]
        self.assertEqual((alert.get("agent"), alert.get("responsible")), (None, "human:alice"))
        router = notify.Router.__new__(notify.Router)
        self.assertEqual(router.resolve(alert), ("human:alice", "alerte"))
        store.work.queue.return_value = [{
            "work_item_id": 5, "target": "main", "state": "queued", "rank": 1,
            "updated_ts": time.time() - 7200, "assignee": "human:alice", "title": "t"}]
        with mock.patch.object(storage, "of", return_value=store):
            blocked = exploitation._merge_blocked(None, time.time())[0]
        self.assertEqual((blocked["agent"], blocked["responsible"]), (None, "human:alice"))


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

    def test_queue_write_fenced_by_the_lease(self):
        """#194 r5: a queue write under a lease happens only while it holds."""
        ops = storage.of(self.db).work
        lot = work.add(self.db, title="fenced")["id"]
        self.assertIsNotNone(ops.queue_add(lot, target="main", frozen_sha="a" * 40,
                                           note="queued", actor="t"))
        one = ops.land_lock("q:main", "r1", 60)
        self.assertIsNotNone(ops.queue_update(lot, state="landing", lease=("q:main", "r1", one)))
        self.assertIsNone(ops.queue_update(lot, state="ejected", lease=("q:main", "r2", one)))
        self.db.execute("UPDATE merge_locks SET expires_at = now() - interval '1 second'"
                        " WHERE target = 'q:main'")
        self.assertIsNone(ops.queue_update(lot, state="ejected", lease=("q:main", "r1", one)))
        two = ops.land_lock("q:main", "r2", 60)
        self.assertIsNone(ops.queue_update(lot, state="ejected", lease=("q:main", "r1", one)))
        self.assertIsNotNone(ops.queue_update(lot, state="landed", lease=("q:main", "r2", two)))
        self.assertEqual(ops.queue()[0]["state"], "landed")
        self.assertIsNotNone(ops.queue_update(lot, reason="radar"))   # no lease: unchanged


    def test_queue_write_fenced_by_the_freeze(self):
        """#194 r8: a merge recorded without its lease writes only over the
        same freeze still `landing` on the same landing commit."""
        ops = storage.of(self.db).work
        lot = work.add(self.db, title="frozen")["id"]
        ops.queue_add(lot, target="main", frozen_sha="a" * 40, note="queued", actor="t")
        self.assertIsNone(ops.queue_update(lot, state="landed", freeze=("a" * 40, None)))
        ops.queue_update(lot, state="landing", landing_sha="c" * 40)
        for other in (("b" * 40, "c" * 40), ("a" * 40, "d" * 40), ("a" * 40, None)):
            self.assertIsNone(ops.queue_update(lot, state="landed", freeze=other))
        self.assertIsNotNone(ops.queue_update(lot, state="landed",
                                              freeze=("a" * 40, "c" * 40)))
        self.assertEqual(ops.queue()[0]["state"], "landed")

    def test_admission_identity_fences_held_and_lost_lease_writes(self):
        ops = storage.of(self.db).work
        lot = work.add(self.db, title="admission identity")["id"]
        frozen = ops.add_milestone(lot, "frozen", sha="a" * 40, actor="t", verdict=None, note="frozen")
        row = ops.queue_add(lot, target="main", frozen_sha="a" * 40,
                            frozen_id=frozen["id"], note="queued", actor="t")
        entry = tuple(row[k] for k in ("generation", "target", "frozen_id", "frozen_sha"))
        epoch = ops.land_lock("id:main", "r1", 60)
        lease = ("id:main", "r1", epoch)
        ops.queue_update(lot, state="landing", landing_sha="c" * 40, lease=lease, entry=entry)
        # Same SHA refrozen without requeue: current milestone must match too.
        newer = ops.add_milestone(lot, "frozen", sha="a" * 40, actor="t", verdict=None, note="again")
        for bail in (lease, None):
            self.assertIsNone(ops.queue_update(lot, state="landed", lease=bail, entry=entry,
                                             freeze=("a" * 40, "c" * 40)))
        self.assertIsNone(ops.queue_add(lot, target="main", frozen_sha="a" * 40,
                                       frozen_id=frozen["id"], note="stale admission", actor="t"))
        row = ops.queue_add(lot, target="main", frozen_sha="a" * 40,
                            frozen_id=newer["id"], note="requeued", actor="t")
        current = tuple(row[k] for k in ("generation", "target", "frozen_id", "frozen_sha"))
        self.assertNotEqual(current[0], entry[0])
        ops.queue_update(lot, state="landing", landing_sha="c" * 40, entry=current)
        for index, value in ((0, entry[0]), (1, "release"), (2, frozen["id"]), (3, "b" * 40)):
            wrong = list(current)
            wrong[index] = value
            for bail in (lease, None):
                self.assertIsNone(ops.queue_update(lot, state="landed", lease=bail, entry=tuple(wrong),
                                                 freeze=("a" * 40, "c" * 40)))
        # Removal/re-entry with identical target, freeze and SHA is distinct.
        ops.queue_remove(lot, note="removed", actor="t")
        row = ops.queue_add(lot, target="main", frozen_sha="a" * 40,
                            frozen_id=newer["id"], note="reentered", actor="t")
        replacement = tuple(row[k] for k in ("generation", "target", "frozen_id", "frozen_sha"))
        ops.queue_update(lot, state="landing", landing_sha="c" * 40, entry=replacement)
        for bail in (lease, None):
            self.assertIsNone(ops.queue_update(lot, state="landed", lease=bail, entry=current,
                                             freeze=("a" * 40, "c" * 40)))
        self.assertIsNotNone(ops.queue_update(lot, state="landed", lease=lease, entry=replacement,
                                              freeze=("a" * 40, "c" * 40)))

    def test_history_query_includes_owner_after_queue_removal(self):
        ops = storage.of(self.db).work
        lot = work.add(self.db, title="historical owner")["id"]
        self.db.execute("UPDATE work_items SET assignee = 'human:alice' WHERE id = %s", (lot,))
        ops.note(lot, "intake", land.UNCERTAIN + "merged (history: uncertain merge)", "t")
        event, = ops.events_noted(land.UNCERTAIN, 0)
        self.assertEqual(event["assignee"], "human:alice")
        alert, = land.uncertain(self.db)
        self.assertEqual(alert["assignee"], "human:alice")

    def test_entry_write_waits_for_a_concurrent_refreeze(self):
        import threading
        ops = storage.of(self.db).work
        lot = work.add(self.db, title="refreeze lock")["id"]
        ops.add_milestone(lot, "frozen", sha="a" * 40, actor="t", verdict=None, note="first")
        row = ops.queue_add(lot, target="main", frozen_sha="a" * 40, note="queued", actor="t")
        entry = tuple(row[k] for k in ("generation", "target", "frozen_id", "frozen_sha"))
        ops.queue_update(lot, state="landing", entry=entry)
        other = self.connect()
        result = {}
        def write():
            result["row"] = ops.queue_update(lot, state="landed", entry=entry, freeze=("a" * 40, None))
        try:
            with other.transaction() as tx:
                tx.query("SELECT id FROM work_items WHERE id = %s FOR NO KEY UPDATE", (lot,))
                tx.execute("INSERT INTO work_item_milestones (work_item_id, kind, sha, actor, note)"
                           " VALUES (%s, 'frozen', %s, 't', 'second')", (lot, "a" * 40))
                writer = threading.Thread(target=write)
                writer.start()
                writer.join(timeout=0.5)
                self.assertTrue(writer.is_alive())
            writer.join(timeout=30)
            self.assertFalse(writer.is_alive())
        finally:
            other.close()
        self.assertIsNone(result.get("row"))
        self.assertEqual(ops.queue()[0]["state"], "landing")

    def test_queue_write_waits_for_the_lease_row(self):
        """#194 r6: the guarded write locks the lease row: an acquisition in
        progress makes it wait, then it sees the new epoch and writes nothing."""
        import threading
        ops = storage.of(self.db).work
        lot = work.add(self.db, title="row lock")["id"]
        ops.queue_add(lot, target="main", frozen_sha="b" * 40, note="queued", actor="t")
        one = ops.land_lock("s:main", "r1", 60)
        other = self.connect()
        result = {}

        def stale_write():
            result["row"] = ops.queue_update(lot, state="ejected", lease=("s:main", "r1", one))
        try:
            with other.transaction() as tx:
                tx.query("UPDATE merge_locks SET owner = 'r2', epoch = epoch + 1"
                         " WHERE target = 's:main' RETURNING epoch")
                writer = threading.Thread(target=stale_write)
                writer.start()
                writer.join(timeout=1.5)
                self.assertTrue(writer.is_alive(), "the write did not wait for the lease row")
            writer.join(timeout=30)
        finally:
            other.close()
        self.assertIsNone(result.get("row"))
        self.assertEqual([r["state"] for r in ops.queue() if r["work_item_id"] == lot],
                         ["queued"])


if __name__ == "__main__":
    unittest.main()
