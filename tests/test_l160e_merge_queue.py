# SPDX-License-Identifier: AGPL-3.0-only
"""L160e1: merge queue per target — entry conditions (freeze, verdict on the
frozen commit, green CI), order (priority, conflicts, arrival), stacked
replay in the radar's clone with the prepared resolutions, ejections and
the resolver woken when its conflict blocks the queue.

Tests without a database first (git repositories, storage mocked), then on
real Postgres (table `merge_queue`, `work queue`, `work show`)."""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from ameesh import mail, merge_queue, plan_github, radar, resolutions, storage, work

from .support import PgTestCase
from .test_l160b_radar import LOTS, MIG, _git, make_repo
from .test_l160d_resolutions import FakeWork, _commit_on, _identity, _manual_resolution


class FakeQueue(FakeWork):
    """`FakeWork` plus the merge queue operations, in memory."""

    def __init__(self, lots):
        super().__init__(lots)
        self.queue_rows: dict = {}
        self.clock = 0.0

    def _row(self, lot):
        q = self.queue_rows.get(lot)
        if q is None:
            return None
        item = self.lots[lot]
        return dict(q, title=item.get("title"), priority=item.get("priority"),
                    item_state=item.get("state"), branch=item.get("branch"),
                    assignee=item.get("assignee"))

    def queue_add(self, item_id, *, target, frozen_sha, note, actor):
        if self.lots[item_id]["state"] in ("merged", "promoted", "closed"):
            return None
        self.clock += 1
        self.queue_rows[item_id] = {"work_item_id": item_id, "target": target,
                                    "frozen_sha": frozen_sha, "state": "queued", "rank": None,
                                    "landing_sha": None,
                                    "reason": "", "queued_by": actor, "queued_ts": self.clock}
        self.notes.append((item_id, note, actor))
        return self._row(item_id)

    def queue_update(self, item_id, *, state=None, rank=None, reason=None, landing_sha=None,
                     note="", actor=""):
        q = self.queue_rows.get(item_id)
        if q is None:
            return None
        if state:
            q["state"] = state
        if state in ("queued", "ejected"):
            q["landing_sha"] = None
        elif landing_sha:
            q["landing_sha"] = landing_sha
        q["rank"] = None if state == "ejected" else (rank if rank is not None else q["rank"])
        if reason is not None:
            q["reason"] = reason
        if note:
            self.notes.append((item_id, note, actor))
        return self._row(item_id)

    def queue_remove(self, item_id, *, note, actor):
        self.notes.append((item_id, note, actor))
        return self.queue_rows.pop(item_id, None) is not None

    def queue(self, target=None):
        rows = [self._row(n) for n in self.queue_rows]
        return sorted((r for r in rows if target in (None, r["target"])),
                      key=lambda r: (r["target"], r["rank"] is None, r["rank"] or 0,
                                     r["queued_ts"]))


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-l160e-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = make_repo(self.tmp)
        _identity(self.repo)
        lots = [dict(r, assignee={1: "a1", 2: "a2", 3: "a3", 4: "a4"}.get(r["id"]))
                for r in LOTS[:4]]
        self.fake = FakeQueue(lots)
        self.marks: dict = {}
        store = mock.Mock(work=self.fake)
        for patch in (mock.patch.object(storage, "of", return_value=store),
                      mock.patch.object(work, "get",
                                        side_effect=lambda db, n: self.fake.lots.get(n)),
                      mock.patch.object(work, "milestones",
                                        side_effect=lambda db, n, *a: self.marks.get(n, []))):
            patch.start()
            self.addCleanup(patch.stop)

    def tip(self, lot):
        return _git(self.repo, "rev-parse", self.fake.lots[lot]["branch"])

    def ready(self, lot, *, verdict="ok", sha=None):
        sha = sha or self.tip(lot)
        self.marks[lot] = [{"id": 2, "kind": "verdict", "verdict": verdict, "sha": sha,
                            "at_ts": 2.0},
                           {"id": 1, "kind": "frozen", "sha": sha, "at_ts": 1.0}]

    def enter(self, lot, ci=None):
        return merge_queue.enter(None, lot, repo=self.repo, actor="orch", ci=ci)


class EntryTest(_Base):
    def test_refusals_then_entry(self):
        with self.assertRaisesRegex(merge_queue.QueueError, "not frozen"):
            self.enter(1)
        self.ready(1, verdict="blocked")
        with self.assertRaisesRegex(merge_queue.QueueError, "no review verdict ok"):
            self.enter(1)
        old = self.tip(1)
        self.ready(1)
        _commit_on(self.repo, "lot/1-a", "other.py", "z\n", "a after the freeze")
        with self.assertRaisesRegex(merge_queue.QueueError, "not at its freeze"):
            self.enter(1)
        self.ready(1)  # frozen again, but the verdict is on the old commit
        self.marks[1][0]["sha"] = old
        with self.assertRaisesRegex(merge_queue.QueueError, "no review verdict ok"):
            self.enter(1)
        self.ready(1)
        with self.assertRaisesRegex(merge_queue.QueueError, "CI of PR #7 is not green"):
            self.enter(1, ci=lambda branch: {"number": 7, "green": False,
                                             "head": self.tip(1)})
        row = self.enter(1, ci=lambda branch: {"number": 7, "green": True,
                                               "head": self.tip(1)})
        self.assertEqual((row["target"], row["frozen_sha"], row["state"]),
                         ("main", self.tip(1), "queued"))
        self.assertIn("PR #7 green", self.fake.notes[-1][1])
        # a team that merges locally has no PR: nothing to check
        self.ready(2)
        self.assertEqual(self.enter(2, ci=lambda branch: None)["state"], "queued")

    def test_sha_prefixes_are_resolved_and_compared_whole(self):
        tip = self.tip(1)
        self.ready(1, sha=tip[:1])  # "a": any tip starting with that letter would match
        with self.assertRaisesRegex(merge_queue.QueueError, "not a commit of this repository"):
            self.enter(1)
        self.ready(1, sha=tip[:12])  # a unique abbreviation is resolved
        self.assertEqual(self.enter(1)["frozen_sha"], tip)
        self.assertFalse(merge_queue._same(tip, tip[:12]))

    def test_green_ci_on_another_commit_is_refused(self):
        self.ready(1)
        other = self.tip(2)
        with self.assertRaisesRegex(merge_queue.QueueError,
                                    "PR #7 is at %s, not at the freeze" % other[:12]):
            self.enter(1, ci=lambda branch: {"number": 7, "green": True, "head": other})

    def test_github_error_refuses_the_lot(self):
        self.ready(1)

        def broken(branch):
            raise plan_github.GithubError("gh pr list : code 4 : authentication required")
        with self.assertRaisesRegex(merge_queue.QueueError, "cannot read the CI"):
            self.enter(1, ci=broken)
        self.assertEqual(self.fake.queue_rows, {})
        # radar.github_ci: strict for the queue, passive (None) for the mail
        gh = mock.Mock(run=mock.Mock(return_value=(4, "", "authentication required")))
        project = radar.Project(name="p", repo=self.repo, github="o/r")
        self.assertIsNone(radar.github_ci(project, gh=gh)("lot/1-a"))
        with self.assertRaises(plan_github.GithubError):
            radar.github_ci(project, gh=gh, strict=True)("lot/1-a")
        gh.run.return_value = (0, json.dumps([{"number": 7, "url": "u", "headRefOid": "abc",
                                               "statusCheckRollup": []}]), "")
        self.assertEqual(radar.github_ci(project, gh=gh, strict=True)("lot/1-a")["head"], "abc")
        self.assertIn("headRefOid", gh.run.call_args[0][0][-1])

    def test_remote_branch_is_fetched_into_the_radar_clone_only(self):
        agents = os.path.join(self.tmp, "agents")
        _git(self.tmp, "clone", "-q", self.repo, agents)
        stale = _git(agents, "rev-parse", "refs/remotes/origin/lot/1-a")
        _commit_on(self.repo, "lot/1-a", "other.py", "z\n", "pushed from another host")
        self.ready(1)  # frozen and reviewed on the fresh tip, unknown to `agents`
        clone = resolutions.Replayer(agents, os.path.join(self.tmp, "state", "p.clone"))
        row = merge_queue.enter(None, 1, repo=agents, actor="orch", ci=None, clone=clone)
        self.assertEqual(row["frozen_sha"], self.tip(1))
        # the agents' repository is untouched: no object, no FETCH_HEAD, same remote ref
        self.assertEqual(_git(agents, "rev-parse", "refs/remotes/origin/lot/1-a"), stale)
        self.assertFalse(os.path.exists(os.path.join(agents, ".git", "FETCH_HEAD")))
        self.assertNotEqual(subprocess.run(["git", "-C", agents, "cat-file", "-e",
                                            self.tip(1) + "^{commit}"],
                                           capture_output=True).returncode, 0)
        # without the clone, the stale origin/<branch> is not the freeze: refused
        self.fake.queue_rows.clear()
        with self.assertRaisesRegex(merge_queue.QueueError, "not at its freeze"):
            merge_queue.enter(None, 1, repo=agents, actor="orch", ci=None)

    def test_merged_lot_is_refused(self):
        self.ready(1)
        self.fake.lots[1]["state"] = "merged"
        with self.assertRaisesRegex(merge_queue.QueueError, "nothing to merge"):
            self.enter(1)


class OrderTest(unittest.TestCase):
    def entries(self, *specs):
        return [{"work_item_id": n, "priority": p, "queued_ts": float(t)}
                for n, p, t in specs]

    def test_priority_then_conflicts_then_arrival(self):
        pairs = {frozenset((1, 2)): {"state": "conflict", "resolver": {"lot": 2}},
                 frozenset((3, 4)): {"state": "prepared"}}
        got = merge_queue.order(self.entries((1, 2, 1), (2, 2, 2), (3, 2, 3), (4, 2, 4),
                                             (5, 1, 5)), pairs)
        # 5 first (priority 1); then 3 and 4 (prepared), before 1 and 2 (not prepared)
        self.assertEqual([o["work_item"] for o in got], [5, 3, 4, 1, 2])
        self.assertEqual([o["rank"] for o in got], [1, 2, 3, 4, 5])
        self.assertEqual(got[2]["reason"], "conflicts with #3 prepared")
        self.assertEqual(got[4]["waits"], [(1, 2)])
        self.assertIn("waits for the resolution of its conflict with #1", got[4]["reason"])

    def test_clean_lots_before_conflicting_ones(self):
        pairs = {frozenset((1, 2)): {"state": "stale"}}
        got = merge_queue.order(self.entries((1, 2, 1), (2, 2, 2), (3, 2, 3)), pairs)
        self.assertEqual([o["work_item"] for o in got], [3, 1, 2])
        self.assertEqual(got[0]["reason"], "priority 2, no conflict in the queue")


class StackTest(_Base):
    def replayer(self):
        rp = resolutions.Replayer(self.repo, os.path.join(self.tmp, "state", "p.clone"))
        rp.ensure()
        return rp

    def lots(self, *ids):
        return [{"work_item": n, "tip": self.tip(n)} for n in ids]

    def test_prepared_conflict_replayed_unprepared_skipped(self):
        main = _git(self.repo, "rev-parse", "main")
        rp = self.replayer()
        got = merge_queue.stack(rp, main, self.lots(1, 2, 4), [])
        self.assertEqual(got[1], {"state": "clean", "files": []})
        self.assertEqual((got[2]["state"], got[2]["files"]), ("conflict", ["shared.py"]))
        # 2 was skipped: 4 is stacked on 1 alone, cleanly
        self.assertEqual(got[4]["state"], "clean")
        sha = _manual_resolution(self.repo, "lot/2-b", "lot/1-a")
        row = {"work_item_id": 2, "other_item_id": 1, "commit_sha": sha}
        got = merge_queue.stack(rp, main, self.lots(1, 2), [row])
        self.assertEqual(got[2], {"state": "replayed", "files": ["shared.py"]})
        # the clone is left clean, the agents' repository untouched
        self.assertEqual(_git(rp.path, "status", "--porcelain"), "")
        self.assertEqual(_git(self.repo, "status", "--porcelain"), "")
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".git", "rr-cache")))

    def test_a_resolution_with_a_lot_behind_does_not_count(self):
        sha = _manual_resolution(self.repo, "lot/2-b", "lot/1-a")
        row = {"work_item_id": 2, "other_item_id": 1, "commit_sha": sha}
        got = merge_queue.stack(self.replayer(), _git(self.repo, "rev-parse", "main"),
                                self.lots(2, 1), [row])
        self.assertEqual(got[2]["state"], "clean")
        self.assertEqual(got[1]["state"], "replayed")  # the pair's resolution, either way


class PassTest(_Base):
    def setUp(self):
        super().setUp()
        self.cfg = dataclasses.make_dataclass("Cfg", ["state_dir"])(self.tmp + "/state")
        self.project = radar.Project(name="p", repo=self.repo, migrations=MIG)
        self.sent: list = []
        patch = mock.patch.object(mail, "send", side_effect=lambda db, s, to, body, **kw:
                                  self.sent.append((to, body, kw)) or len(self.sent))
        patch.start()
        self.addCleanup(patch.stop)

    def run_pass(self, cache=None):
        lots = [dict(r) for r in self.fake.lots.values()]
        report = radar.scan(self.repo, lots, migrations=MIG, cache=cache)
        radar.annotate_resolutions(self.cfg, None, self.project, report, lots, cache)
        report["queue"] = radar.run_queue(self.cfg, None, self.project, report, cache)
        return report

    def queue(self, *ids):
        for n in ids:
            self.ready(n)
            self.enter(n)

    def test_unprepared_conflict_ejects_and_wakes(self):
        self.queue(1, 2, 4)
        report = self.run_pass()
        listed = report["queue"]["targets"]["main"]
        self.assertEqual([(o["lot"], o["state"]) for o in listed],
                         [(4, "queued"), (1, "queued"), (2, "ejected")])
        self.assertEqual(self.fake.queue_rows[2]["state"], "ejected")
        self.assertIn("shared.py", self.fake.queue_rows[2]["reason"])
        woken = [(to, kw["payload"]["merge_queue"].get("kind")) for to, _, kw in self.sent]
        # 2, the resolver (newer lot), is ejected: one mail, which says to prepare it
        self.assertEqual(woken, [("a2", "ejected")])
        self.assertTrue(all(kw["kind"] == "event" for _, _, kw in self.sent))
        self.assertIn("file vers main : 1. #4, 2. #1, 3. #2 (éjecté)",
                      "\n".join(radar.render(report)))
        # next pass: 2 is out, 1 conflicts with nothing left (arrival order), nothing sent
        self.sent.clear()
        report = self.run_pass()
        self.assertEqual([o["lot"] for o in report["queue"]["targets"]["main"]], [1, 4])
        self.assertEqual(self.sent, [])

    def test_prepared_conflict_stays_in_the_queue(self):
        sha = _manual_resolution(self.repo, "lot/2-b", "lot/1-a")
        self.fake.record_resolution(2, 1, commit=sha, lot_tip="x", other_tip="y",
                                    ref=None, files=["shared.py"], note="", actor="a2")
        self.queue(1, 2)
        cache = radar.Cache()
        report = self.run_pass(cache)
        listed = report["queue"]["targets"]["main"]
        self.assertEqual([(o["lot"], o["state"], o["stack"]["state"]) for o in listed],
                         [(1, "queued", "clean"), (2, "queued", "replayed")])
        self.assertEqual([self.fake.queue_rows[n]["rank"] for n in (1, 2)], [1, 2])
        self.assertEqual(self.sent, [])
        # cached: nothing moved, no new replay
        with mock.patch.object(merge_queue, "stack",
                               side_effect=AssertionError("stacked again")):
            self.run_pass(cache)

    def test_push_past_the_freeze_ejects(self):
        self.queue(1)
        _commit_on(self.repo, "lot/1-a", "other.py", "z\n", "a pushes while queued")
        self.run_pass()
        self.assertEqual(self.fake.queue_rows[1]["state"], "ejected")
        self.assertIn("moved past its freeze", self.fake.queue_rows[1]["reason"])
        self.assertEqual([to for to, _, _ in self.sent], ["a1"])

    def test_merged_lot_lands(self):
        self.queue(4)
        self.fake.lots[4]["state"] = "merged"
        self.run_pass()
        self.assertEqual(self.fake.queue_rows[4]["state"], "landed")

    def test_conflict_with_the_target_ejects(self):
        self.queue(4, 1)
        _commit_on(self.repo, "main", "shared.py",
                   "Uno\ndeux\ntrois\nquatre\ncinq\nsix\nsept\n", "main moves")
        report = self.run_pass()
        self.assertEqual([(o["lot"], o["state"]) for o in report["queue"]["targets"]["main"]],
                         [(4, "queued")])
        self.assertEqual(self.fake.queue_rows[1]["state"], "ejected")
        self.assertIn("conflict with main on shared.py", self.fake.queue_rows[1]["reason"])
        self.assertEqual([(to, kw["payload"]["merge_queue"]["kind"]) for to, _, kw in self.sent],
                         [("a1", "ejected")])
        self.assertIn("Integrate main", self.sent[0][1])

    def test_ejected_resolver_gets_no_wait_mail(self):
        """Pairs 1–2 and 2–3, both resolved by 2; 2 is ejected after 1: one
        mail to 2 (its ejection), none saying that 3 waits on it."""
        tips = {n: str(n) * 40 for n in (1, 2, 3)}
        for n in (1, 2, 3):
            self.fake.queue_add(n, target="main", frozen_sha=tips[n], note="", actor="o")
        report = {
            "branches": [{"work_item": n, "tip": tips[n], "target": "main",
                          "target_head": "f" * 40, "branch": "lot/%d" % n} for n in (1, 2, 3)],
            "pairs": [{"lots": [1, 2], "state": "conflict", "resolver": {"lot": 2}},
                      {"lots": [2, 3], "state": "conflict", "resolver": {"lot": 2}}],
            "against_target": []}
        trial = {1: {"state": "clean", "files": []},
                 2: {"state": "conflict", "files": ["shared.py"]},
                 3: {"state": "clean", "files": []}}
        with mock.patch.object(merge_queue, "stack", return_value=trial):
            out = merge_queue.run_pass(self.cfg, None, self.project, report,
                                       replayer=mock.Mock())
        self.assertEqual([(o["lot"], o["state"]) for o in out["targets"]["main"]],
                         [(1, "queued"), (2, "ejected"), (3, "queued")])
        self.assertEqual([(m["lot"], m["kind"]) for m in out["mail"]], [(2, "ejected")])
        self.assertEqual([to for to, _, _ in self.sent], ["a2"])

    def test_database_without_the_table(self):
        with mock.patch.object(self.fake, "queue", side_effect=RuntimeError(
                'relation "merge_queue" does not exist')):
            report = self.run_pass()
        self.assertIn("merge_queue", report["queue"]["error"])
        self.assertIn("file : RuntimeError", "\n".join(radar.render(report)))


class QueueDbTest(PgTestCase):
    """Table `merge_queue` (0058), `work queue`, `work show`."""

    def test_storage_and_cli(self):
        a = work.add(self.db, title="a", branch="lot/1-a", priority=1)["id"]
        b = work.add(self.db, title="b", branch="lot/2-b")["id"]
        ops = storage.of(self.db).work
        row = ops.queue_add(a, target="main", frozen_sha="1" * 40, note="queued", actor="o")
        self.assertEqual((row["state"], row["rank"], row["priority"], row["title"]),
                         ("queued", None, 1, "a"))
        ops.queue_add(b, target="main", frozen_sha="2" * 40, note="queued", actor="o")
        ops.queue_update(b, rank=1, reason="first")
        ops.queue_update(a, rank=2, reason="second")
        self.assertEqual([r["work_item_id"] for r in ops.queue("main")], [b, a])
        self.assertEqual(ops.queue("develop"), [])
        landing = ops.queue_update(b, state="landing", landing_sha="5" * 40)
        self.assertEqual((landing["state"], landing["landing_sha"], landing["rank"]),
                         ("landing", "5" * 40, 1))
        ejected = ops.queue_update(b, state="ejected", reason="conflict", note="ejected",
                                   actor="ameesh")
        self.assertEqual((ejected["state"], ejected["rank"], ejected["landing_sha"]),
                         ("ejected", None, None))
        again = ops.queue_add(b, target="main", frozen_sha="3" * 40, note="again", actor="o")
        self.assertEqual((again["state"], again["frozen_sha"], again["reason"]),
                         ("queued", "3" * 40, ""))
        self.assertIsNone(ops.queue_update(999999, rank=1))
        notes = [e["note"] for e in work.events(self.db, b)]
        self.assertIn("ejected", notes)
        shown = self.mesh("work", "show", str(a))
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn("file     : queued vers main, rang 2", shown.stdout)
        listed = self.mesh("work", "queue", "show")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn("queue of main:", listed.stdout)
        removed = self.mesh("work", "queue", "remove", str(b), "--json")
        self.assertEqual(json.loads(removed.stdout), {"lot": b, "removed": True})
        self.assertFalse(ops.queue_remove(b, note="", actor=""))

    def test_queue_add_refused_without_freeze(self):
        repo = make_repo(self.tmp)
        lot = work.add(self.db, title="a", branch="lot/1-a")["id"]
        out = self.mesh("work", "queue", "add", str(lot), "--repo", repo)
        self.assertEqual(out.returncode, 1)
        self.assertIn("is not frozen", out.stderr)


if __name__ == "__main__":
    unittest.main()
