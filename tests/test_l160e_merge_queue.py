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

from ameesh import lot_class, mail, merge_queue, plan_github, radar, resolutions, storage, work

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

    def queue_add(self, item_id, *, target, frozen_sha, note, actor, source="", frozen_id=None):
        if self.lots[item_id]["state"] in ("merged", "promoted", "closed"):
            return None
        self.clock += 1
        self.queue_rows[item_id] = {"work_item_id": item_id, "target": target,
                                    "frozen_sha": frozen_sha, "state": "queued", "rank": None,
                                    "landing_sha": None,
                                    "reason": "", "queued_by": actor, "queued_ts": self.clock,
                                    "source": source, "generation": self.clock, "frozen_id": frozen_id}
        self.notes.append((item_id, note, actor))
        return self._row(item_id)

    def queue_update(self, item_id, *, state=None, rank=None, reason=None, landing_sha=None,
                     note="", actor="", clear_rank=False, lease=None, freeze=None, entry=None):
        q = self.queue_rows.get(item_id)
        if q is None:
            return None
        if lease is not None and not getattr(self, "lease_ok", lambda lease: True)(lease):
            return None
        if entry is not None and tuple(q.get(k) for k in (
                "generation", "target", "frozen_id", "frozen_sha")) != entry:
            return None
        if freeze is not None and (q["state"], q["frozen_sha"], q.get("landing_sha")) != (
                "landing", *freeze):
            return None
        if state:
            q["state"] = state
        if state in ("queued", "ejected"):
            q["landing_sha"] = None
        elif landing_sha:
            q["landing_sha"] = landing_sha
        q["rank"] = None if state == "ejected" or clear_rank else (
            rank if rank is not None else q["rank"])
        if reason is not None:
            q["reason"] = reason
        if note:
            self.notes.append((item_id, note, actor))
        return self._row(item_id)

    def note(self, item_id, state, text, actor):
        self.notes.append((item_id, text, actor))

    def events_noted(self, prefix, since_ts):
        return [{"work_item_id": i, "note": n, "actor": a, "created_ts": self.clock, "assignee": self.lots[i].get("assignee")}
                for i, n, a in self.notes if n.startswith(prefix)]

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
        with self.assertRaisesRegex(merge_queue.QueueError, "not a commit id"):
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
        # without the clone, origin cannot be fetched: refused, never the cached tip
        self.fake.queue_rows.clear()
        with self.assertRaisesRegex(merge_queue.QueueError, "without the radar's clone"):
            merge_queue.enter(None, 1, repo=agents, actor="orch", ci=None)

    def test_a_local_branch_is_not_the_tip_when_origin_moved(self):
        agents = os.path.join(self.tmp, "agents")
        _git(self.tmp, "clone", "-q", self.repo, agents)
        _git(agents, "branch", "lot/1-a", "origin/lot/1-a")  # local copy, reviewed at A
        self.ready(1)
        _commit_on(self.repo, "lot/1-a", "other.py", "z\n", "B, pushed from another host")
        clone = resolutions.Replayer(agents, os.path.join(self.tmp, "state", "p.clone"))
        with self.assertRaisesRegex(merge_queue.QueueError, "not at its freeze"):
            merge_queue.enter(None, 1, repo=agents, actor="orch", ci=None, clone=clone)
        self.assertEqual(self.fake.queue_rows, {})

    def test_milestones_name_commit_ids_only(self):
        tip = self.tip(1)
        for ref in ("HEAD", "lot/1-a", "lot/1-a^0"):
            with self.subTest(ref):
                self.ready(1, sha=ref)  # resolves to the tip today, to a later push tomorrow
                with self.assertRaisesRegex(merge_queue.QueueError, "not a commit id"):
                    self.enter(1)
        self.ready(1)
        self.marks[1][0]["sha"] = "lot/1-a"  # a verdict on the branch, not on a commit
        with self.assertRaisesRegex(merge_queue.QueueError, "no review verdict ok"):
            self.enter(1)
        # a branch named like a commit id is not that commit
        _git(self.repo, "branch", "deadbeef", tip)
        self.ready(1, sha="deadbeef")
        with self.assertRaisesRegex(merge_queue.QueueError, "not a commit of this repository"):
            self.enter(1)
        self.ready(1, sha=tip.upper())
        self.assertEqual(self.enter(1)["frozen_sha"], tip)

    def test_a_failed_fetch_refuses_the_lot(self):
        agents = os.path.join(self.tmp, "agents")
        _git(self.tmp, "clone", "-q", self.repo, agents)
        clone = resolutions.Replayer(agents, os.path.join(self.tmp, "state", "p.clone"))
        self.ready(1)
        merge_queue.enter(None, 1, repo=agents, actor="orch", ci=None, clone=clone)
        # the clone holds the frozen tip; the branch is then deleted on the remote,
        # or the remote does not answer: the stale tip is never admitted
        _git(self.repo, "checkout", "-q", "main")
        _git(self.repo, "branch", "-q", "-D", "lot/1-a")
        for url in (self.repo, os.path.join(self.tmp, "nowhere")):
            with self.subTest(url):
                _git(agents, "remote", "set-url", "origin", url)
                self.fake.queue_rows.clear()
                with self.assertRaisesRegex(merge_queue.QueueError, "cannot fetch branch lot/1-a"):
                    merge_queue.enter(None, 1, repo=agents, actor="orch", ci=None, clone=clone)
                self.assertEqual(self.fake.queue_rows, {})

    def test_merged_lot_is_refused(self):
        self.ready(1)
        self.fake.lots[1]["state"] = "merged"
        with self.assertRaisesRegex(merge_queue.QueueError, "nothing to merge"):
            self.enter(1)


class LightClassTest(_Base):
    """L539 (§8): a light lot enters without a review verdict, but keeps every
    other gate — green CI on the frozen head, a non-draft PR, a target that is
    not a release, and no doubt about the class (`sensitive` otherwise)."""

    def freeze(self, lot):
        tip = self.tip(lot)
        self.marks[lot] = [{"id": 1, "kind": "frozen", "sha": tip, "at_ts": 1.0}]
        return tip

    def ci(self, lot, *, draft=False, green=True, number=7):
        tip = self.tip(lot)
        return lambda branch: {"number": number, "green": green, "head": tip,
                               "draft": draft}

    def light(self, why="policy of /canon"):
        return mock.patch.object(lot_class, "of_lot",
                                 return_value={"class": "light", "why": why})

    def test_a_light_lot_enters_without_a_verdict(self):
        self.freeze(1)
        with self.light():
            row = self.enter(1, ci=self.ci(1))
        self.assertEqual(row["state"], "queued")
        self.assertEqual(row["class"], "light")
        self.assertIn("class light", self.fake.notes[-1][1])

    def test_a_light_lot_is_refused_from_a_draft_pr(self):
        self.freeze(1)
        with self.light():
            with self.assertRaisesRegex(merge_queue.QueueError, "PR #7 is a draft"):
                self.enter(1, ci=self.ci(1, draft=True))
        self.assertEqual(self.fake.queue_rows, {})

    def test_a_light_lot_still_needs_a_green_ci(self):
        self.freeze(1)
        with self.light():
            with self.assertRaisesRegex(merge_queue.QueueError, "CI of PR #7 is not green"):
                self.enter(1, ci=self.ci(1, green=False))
        self.assertEqual(self.fake.queue_rows, {})

    def test_a_release_lot_keeps_the_verdict(self):
        # the release axis (target release/* or lot type release) is read by
        # lot_class; whatever the policy says of the files, release is never light
        self.freeze(1)
        release = {"class": "release", "why": "target release/1.10.0"}
        with mock.patch.object(lot_class, "of_lot", return_value=release):
            with self.assertRaisesRegex(merge_queue.QueueError,
                                        r"no review verdict ok.*class: release"):
                self.enter(1, ci=self.ci(1))
        self.ready(1)  # a verdict ok on the freeze
        with mock.patch.object(lot_class, "of_lot", return_value=release):
            self.assertEqual(self.enter(1, ci=self.ci(1))["state"], "queued")

    def test_a_doubt_keeps_the_verdict_and_names_the_class(self):
        self.freeze(1)
        with mock.patch.object(lot_class, "of_lot", return_value={
                "class": "sensitive", "why": "no readable canon"}):
            with self.assertRaisesRegex(merge_queue.QueueError, "class: sensitive"):
                self.enter(1, ci=self.ci(1))

    def test_without_a_readable_class_the_verdict_is_still_required(self):
        # no patch: with no canon configured, lot_class.of_lot answers
        # `sensitive` itself and the queue must not take the doubt for a light lot
        self.freeze(1)
        with self.assertRaisesRegex(merge_queue.QueueError, r"class: sensitive"):
            self.enter(1, ci=self.ci(1))

    def test_the_queue_line_shows_the_class(self):
        rows = [{"work_item_id": 1, "target": "main", "state": "queued", "rank": 1,
                 "title": "t", "assignee": "a1", "frozen_sha": "a" * 12, "reason": ""}]
        self.assertIn("class light", merge_queue.show_lines(rows, {1: "light"})[1])


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

    def test_a_lot_already_in_the_target_does_not_stop_the_replay(self):
        # codex #188 r6: the fresh target already holds lot 1; the lots after
        # it are still tried
        _git(self.repo, "checkout", "-q", "main")
        _git(self.repo, "merge", "-q", "--no-ff", "-m", "1 lands", "lot/1-a")
        main = _git(self.repo, "rev-parse", "main")
        got = merge_queue.stack(self.replayer(), main, self.lots(1, 4), [])
        self.assertEqual(got[1], {"state": "contained", "files": []})
        self.assertEqual(got[4]["state"], "clean")

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

    def run_pass(self, cache=None, repo=None):
        lots = [dict(r) for r in self.fake.lots.values()]
        report = radar.scan(repo or self.repo, lots, migrations=MIG, cache=cache)
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

    def test_stale_origin_of_the_agents_does_not_eject(self):
        agents = os.path.join(self.tmp, "agents")
        _git(self.tmp, "clone", "-q", self.repo, agents)
        _commit_on(self.repo, "lot/1-a", "other.py", "z\n", "pushed from another host")
        self.ready(1)  # frozen on the fresh tip; the agents still see the old one
        clone = resolutions.Replayer(agents, radar.clone_dir(self.cfg, "p"))
        merge_queue.enter(None, 1, repo=agents, actor="orch", ci=None, clone=clone)
        self.project = radar.Project(name="p", repo=agents, migrations=MIG)
        report = self.run_pass(repo=agents)
        self.assertEqual(self.fake.queue_rows[1]["state"], "queued")
        self.assertEqual([(o["lot"], o["state"])
                          for o in report["queue"]["targets"]["origin/main"]], [(1, "queued")])
        self.assertEqual(self.fake.queue_rows[1]["rank"], 1)
        # the remote does not answer: neither ejected nor ranked, the next pass
        # decides; its rank is cleared, so that no other lot shares it
        # (the same origin, gone for a while: its URL still names the source)
        os.rename(self.repo, self.repo + ".away")
        self.addCleanup(lambda: os.path.isdir(self.repo + ".away")
                        and os.rename(self.repo + ".away", self.repo))
        report = self.run_pass(repo=agents)
        self.assertEqual(self.fake.queue_rows[1]["state"], "queued")
        self.assertIsNone(self.fake.queue_rows[1]["rank"])
        self.assertIn("deferred: cannot fetch", self.fake.queue_rows[1]["reason"])
        self.assertEqual([d["lot"] for d in report["queue"]["deferred"]], [1])
        self.assertEqual(self.sent, [])

    def test_a_push_unseen_by_the_agents_ejects(self):
        """Queued at A; another host pushes B; the agents' cached origin still
        says A: the pass fetches, sees B, and ejects."""
        agents = os.path.join(self.tmp, "agents")
        _git(self.tmp, "clone", "-q", self.repo, agents)
        self.ready(1)
        clone = resolutions.Replayer(agents, radar.clone_dir(self.cfg, "p"))
        merge_queue.enter(None, 1, repo=agents, actor="orch", ci=None, clone=clone)
        _commit_on(self.repo, "lot/1-a", "other.py", "z\n", "B, pushed from another host")
        self.project = radar.Project(name="p", repo=agents, migrations=MIG)
        self.run_pass(repo=agents)
        self.assertEqual(self.fake.queue_rows[1]["state"], "ejected")
        self.assertIn("moved past its freeze", self.fake.queue_rows[1]["reason"])

    def agents_queue(self, lot):
        """`lot` queued from a clone of `self.repo` (its origin), whose
        refs then stay as they were; the pass runs on that clone."""
        agents = os.path.join(self.tmp, "agents")
        _git(self.tmp, "clone", "-q", self.repo, agents)
        self.ready(lot)
        clone = resolutions.Replayer(agents, radar.clone_dir(self.cfg, "p"))
        merge_queue.enter(None, lot, repo=agents, actor="orch", ci=None, clone=clone)
        self.project = radar.Project(name="p", repo=agents, migrations=MIG)
        return agents

    def test_a_lot_absent_from_the_scan_is_fetched_all_the_same(self):
        """Codex #188 r4: the agents' checkout recreated without the lot's
        refs; the queue still fetches it, keeps it ranked while it holds,
        ejects it on an unreviewed push, defers it once origin drops it."""
        agents = self.agents_queue(1)
        _git(agents, "update-ref", "-d", "refs/remotes/origin/lot/1-a")
        report = self.run_pass(repo=agents)
        self.assertNotIn(1, [b["work_item"] for b in report["branches"]])
        self.assertEqual([(o["lot"], o["state"])
                          for o in report["queue"]["targets"]["origin/main"]], [(1, "queued")])
        self.assertEqual(self.fake.queue_rows[1]["rank"], 1)
        _git(self.repo, "checkout", "-q", "main")
        _git(self.repo, "branch", "-q", "-D", "lot/1-a")
        self.run_pass(repo=agents)
        self.assertEqual(self.fake.queue_rows[1]["state"], "queued")
        self.assertIsNone(self.fake.queue_rows[1]["rank"])
        self.assertEqual(self.fake.queue_rows[1]["reason"],
                         "deferred: branch lot/1-a not found on origin")
        _git(self.repo, "branch", "-q", "lot/1-a", self.fake.queue_rows[1]["frozen_sha"])
        _commit_on(self.repo, "lot/1-a", "other.py", "z\n", "B, unreviewed, pushed elsewhere")
        self.run_pass(repo=agents)
        self.assertEqual(self.fake.queue_rows[1]["state"], "ejected")
        self.assertIn("moved past its freeze", self.fake.queue_rows[1]["reason"])
        self.assertEqual([to for to, _, _ in self.sent], ["a1"])

    def test_an_unrelated_push_with_checkouts_recreated_ejects(self):
        """Codex #188 r5: the branch is force-pushed to an unrelated B (no
        common base: the scan skips it) and every checkout is recreated
        without the frozen A; the lot is still this project's (its source),
        so it is ejected, not left with its rank."""
        agents = self.agents_queue(1)
        _git(self.repo, "checkout", "-q", "--orphan", "unrelated")
        _git(self.repo, "rm", "-rqf", ".")
        with open(os.path.join(self.repo, "b.txt"), "w") as fh:
            fh.write("b\n")
        _git(self.repo, "add", "b.txt")
        _git(self.repo, "commit", "-qm", "B, unrelated")
        _git(self.repo, "branch", "-f", "lot/1-a", "unrelated")
        _git(self.repo, "checkout", "-q", "main")
        frozen = self.fake.queue_rows[1]["frozen_sha"]
        _git(self.repo, "reflog", "expire", "--expire=now", "--all")
        _git(self.repo, "gc", "-q", "--prune=now")
        shutil.rmtree(agents)
        shutil.rmtree(radar.clone_dir(self.cfg, "p"), True)
        _git(self.tmp, "clone", "-q", self.repo, agents)
        report = self.run_pass(repo=agents)
        for repo in (self.repo, agents, radar.clone_dir(self.cfg, "p")):
            self.assertNotEqual(subprocess.run(["git", "cat-file", "-e", frozen + "^{commit}"],
                                               cwd=repo, capture_output=True).returncode, 0)
        self.assertNotIn(1, [b["work_item"] for b in report["branches"]])
        self.assertEqual(self.fake.queue_rows[1]["state"], "ejected")
        self.assertIn("moved past its freeze", self.fake.queue_rows[1]["reason"])

    def test_another_project_sharing_the_commit_leaves_the_lot_alone(self):
        """Codex #188 r5: a second project whose repository holds the frozen
        commit (a fork) does not own the lot: its pass keeps the rank."""
        self.queue(1)
        self.run_pass()
        self.assertEqual(self.fake.queue_rows[1]["rank"], 1)
        fork = os.path.join(self.tmp, "fork")
        _git(self.tmp, "clone", "-q", self.repo, fork)
        _git(fork, "remote", "remove", "origin")
        self.assertTrue(_git(fork, "cat-file", "-t", self.fake.queue_rows[1]["frozen_sha"]))
        self.project = radar.Project(name="q", repo=fork, migrations=MIG)
        report = self.run_pass(repo=fork)
        self.assertEqual(report["queue"]["targets"], {})
        self.assertEqual((self.fake.queue_rows[1]["state"], self.fake.queue_rows[1]["rank"],
                          self.fake.queue_rows[1]["reason"]),
                         ("queued", 1, self.fake.queue_rows[1]["reason"]))
        self.assertNotIn("deferred", self.fake.queue_rows[1]["reason"])

    def test_a_conflict_with_the_cached_target_only_does_not_eject(self):
        """Codex #188 r5: the radar finds a conflict against the agents'
        cached target T; origin's target U merges cleanly: the lot stays,
        tried on U."""
        agents = self.agents_queue(1)
        _git(agents, "checkout", "-q", "-b", "stale", "origin/main")
        with open(os.path.join(agents, "shared.py"), "w") as fh:
            fh.write("Uno\ndeux\ntrois\nquatre\ncinq\nsix\nsept\n")
        _git(agents, "commit", "-qam", "T, only in the agents' cache")
        _git(agents, "update-ref", "refs/remotes/origin/main", "stale")
        report = self.run_pass(repo=agents)
        self.assertEqual([a["state"] for a in report["against_target"]
                          if int(a["work_item"]) == 1], ["conflict"])
        self.assertEqual((self.fake.queue_rows[1]["state"], self.fake.queue_rows[1]["rank"]),
                         ("queued", 1))
        self.assertEqual([(o["lot"], o["state"], o["stack"]["state"])
                          for o in report["queue"]["targets"]["origin/main"]],
                         [(1, "queued", "clean")])

    def test_source_of_a_repository(self):
        self.assertEqual(merge_queue.source_of(self.repo), os.path.realpath(self.repo))
        for url in ("git@github.com:Manaty/ameesh.git", "https://github.com/Manaty/ameesh",
                    "ssh://git@GitHub.com:22/Manaty/ameesh.git/"):
            with self.subTest(url):
                _git(self.repo, "remote", "add", "origin", url)
                self.assertEqual(merge_queue.source_of(self.repo), "github.com/manaty/ameesh")
                _git(self.repo, "remote", "remove", "origin")
        # r6: two servers on one host, by their ports, are two sources
        seen = []
        for url in ("ssh://git@host:2222/team/repo.git", "ssh://git@host:2223/team/repo.git",
                    "https://host:443/team/repo"):
            _git(self.repo, "remote", "add", "origin", url)
            seen.append(merge_queue.source_of(self.repo))
            _git(self.repo, "remote", "remove", "origin")
        self.assertEqual(seen, ["host:2222/team/repo", "host:2223/team/repo", "host/team/repo"])

    def test_the_stacked_trial_runs_on_the_fresh_target(self):
        """Codex #188 r4: origin's main moves to a conflicting commit while
        the agents' cached origin/main stays: the trial uses origin's."""
        agents = self.agents_queue(1)
        _commit_on(self.repo, "main", "shared.py",
                   "Uno\ndeux\ntrois\nquatre\ncinq\nsix\nsept\n", "main moves elsewhere")
        report = self.run_pass(repo=agents)
        # the radar, on the agents' stale origin/main, finds lot 1 clean
        self.assertEqual([a["state"] for a in report["against_target"]
                          if int(a["work_item"]) == 1], ["clean"])
        self.assertEqual(self.fake.queue_rows[1]["state"], "ejected")
        self.assertIn("stacked trial: conflict not covered on shared.py",
                      self.fake.queue_rows[1]["reason"])
        # origin's main unreachable: deferred, never a trial on the cached one
        self.fake.queue_rows.clear()
        self.ready(1)
        self.fake.queue_add(1, target="origin/main", frozen_sha=self.tip(1), note="",
                            actor="o")
        with mock.patch.object(merge_queue, "stack",
                               side_effect=AssertionError("stacked on a guess")):
            _git(self.repo, "branch", "-q", "-m", "main", "trunk")
            self.run_pass(repo=agents)
        self.assertIsNone(self.fake.queue_rows[1]["rank"])
        self.assertEqual(self.fake.queue_rows[1]["reason"],
                         "deferred: target origin/main not found on origin")

    def test_another_projects_lot_is_left_alone(self):
        agents = self.agents_queue(1)
        self.fake.lots[3]["branch"] = "lot/3-elsewhere"
        self.fake.queue_add(3, target="origin/main", frozen_sha="3" * 40, note="", actor="o")
        self.fake.queue_rows[3]["reason"] = "ranked by its own project"
        report = self.run_pass(repo=agents)
        self.assertEqual([o["lot"] for o in report["queue"]["targets"]["origin/main"]], [1])
        self.assertNotIn("deferred", report["queue"])
        self.assertEqual((self.fake.queue_rows[3]["state"], self.fake.queue_rows[3]["reason"]),
                         ("queued", "ranked by its own project"))

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
        by_branch = {self.fake.lots[n]["branch"]: tips[n] for n in (1, 2, 3)}
        with mock.patch.object(merge_queue, "stack", return_value=trial), \
                mock.patch.object(merge_queue, "fetch_fresh",
                                  side_effect=lambda repo, names, clone: (
                                      dict(by_branch, main="f" * 40), repo)):
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
        self.assertIn("queue    : queued to main, rank 2", shown.stdout)
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
