# SPDX-License-Identifier: AGPL-3.0-only
"""#3080: preserve fire only for the queue's proven landing; admit commit CI."""
from __future__ import annotations

import dataclasses
from unittest import mock

from ameesh import fire, merge_queue, plan_github, radar, resolutions

from . import test_l160e_merge_queue as queue_tests
from . import test_l160e2_land as land_tests
from .test_l160b_radar import _git


class FireLandingTest(land_tests._Land):
    LAND = "pr"

    def setUp(self):
        super().setUp()
        self.queued(4)
        self.frozen = self.tip(4)
        self.marks[4].append({"id": 4, "kind": "fire", "sha": self.frozen,
                              "at_ts": 4.0, "actor": "orch"})
        self.gh = land_tests.FakeGh()
        self.gh.head = self.frozen
        patches = [mock.patch.object(fire, "_repo_of", return_value=self.repo),
                   mock.patch.object(fire, "_same_repository", return_value=True),
                   mock.patch.object(fire, "_slug", return_value="o/r"),
                   mock.patch.object(fire, "_project", return_value=self.project),
                   mock.patch.object(fire, "_repo_info", return_value={"default_branch": "main"}),
                   mock.patch.object(fire, "_pr_status", side_effect=lambda *a, **kw: {
                       "number": 7, "head": self.gh.head, "base": "main", "state": "OPEN"}),
                   mock.patch.object(fire, "ready_verdicts",
                                     return_value=(True, "", {"class": "sensitive"})),
                   mock.patch.object(fire, "_invalidate", return_value={"result": "invalidated"})]
        for patch in patches:
            started = patch.start()
            self.addCleanup(patch.stop)
        self.invalidated = started

    def act(self):
        return fire._act(self.cfg, None, self.fake.lots[4], self.marks[4],
                         gh=self.gh, dry_run=False, repo=None, cache={})

    def start_landing(self):
        out = self.land(4, gh=self.gh)
        self.assertEqual(out["result"], "landing", out)
        self.gh.head = out["sha"]
        return out["sha"]

    def test_fire_survives_queue_push_and_waits_for_pr_ci(self):
        self.start_landing()
        self.assertEqual(self.act()["result"], "landing")
        self.invalidated.assert_not_called()
        self.assertEqual(fire.active(self.marks[4])["sha"], self.frozen)
        self.assertEqual(self.fake.queue_rows[4]["frozen_sha"], self.frozen)
        self.gh.ci = "green"
        self.assertEqual(self.land(4, gh=self.gh)["result"], "landed")

    def test_identity_is_published_before_fire_can_see_the_push(self):
        run = resolutions._run
        observed = []

        def observe(path, *args, **kw):
            result = run(path, *args, **kw)
            if args[:1] == ("push",) and any("refs/heads/lot/4-d" in a for a in args):
                self.gh.head = _git(self.origin, "rev-parse", "lot/4-d")
                observed.append(self.act()["result"])
            return result

        with mock.patch.object(resolutions, "_run", side_effect=observe):
            self.start_landing()
        self.assertEqual(observed, ["landing"])
        self.invalidated.assert_not_called()

    def test_third_party_push_still_invalidates(self):
        self.start_landing()
        _git(self.repo, "fetch", "-q", "origin", "lot/4-d")
        extra = _git(self.repo, "commit-tree", "FETCH_HEAD^{tree}", "-p", "FETCH_HEAD",
                     "-m", "third-party push")
        _git(self.repo, "push", "-q", "origin", extra + ":lot/4-d")
        self.gh.head = extra
        self.assertEqual(self.act()["result"], "invalidated")

    def test_queue_identity_must_match(self):
        sha = self.start_landing()
        entry = self.fake.queue_rows[4]
        for field, value in (("state", "queued"), ("landing_sha", self.frozen),
                             ("frozen_sha", sha), ("frozen_id", 999),
                             ("source", "other/repo"), ("target", "other")):
            with self.subTest(field=field), mock.patch.dict(entry, {field: value}):
                self.assertEqual(self.act()["result"], "invalidated")

    def test_same_parents_with_extra_tree_edits_are_refused(self):
        sha = self.start_landing()
        clone = radar.clone_dir(self.cfg, "p")
        parents = _git(clone, "show", "-s", "--format=%P", sha).split()
        wrong = _git(clone, "commit-tree", parents[0] + "^{tree}",
                     "-p", parents[0], "-p", parents[1], "-m", "extra edit")
        _git(clone, "push", "-q", "--force", "origin", wrong + ":lot/4-d")
        self.fake.queue_rows[4]["landing_sha"] = wrong
        self.gh.head = wrong
        self.assertEqual(self.act()["result"], "invalidated")

    def test_wrong_parent_order_is_refused_even_with_the_right_tree(self):
        sha = self.start_landing()
        clone = radar.clone_dir(self.cfg, "p")
        parents = _git(clone, "show", "-s", "--format=%P", sha).split()
        wrong = _git(clone, "commit-tree", sha + "^{tree}",
                     "-p", parents[1], "-p", parents[0], "-m", "reversed parents")
        _git(clone, "push", "-q", "--force", "origin", wrong + ":lot/4-d")
        self.fake.queue_rows[4]["landing_sha"] = wrong
        self.gh.head = wrong
        self.assertEqual(self.act()["result"], "invalidated")

    def test_target_moved_is_not_a_valid_landing(self):
        self.start_landing()
        _git(self.repo, "push", "-q", "origin", "lot/3-c:main")
        self.assertEqual(self.act()["result"], "invalidated")

    def test_transient_fetch_does_not_withdraw_fire(self):
        self.start_landing()
        with mock.patch.object(merge_queue, "fetch_fresh",
                               side_effect=merge_queue.QueueError("remote unavailable")):
            self.assertEqual(self.act()["result"], "not-ready")
        self.invalidated.assert_not_called()


class CommitAdmissionTest(queue_tests._Base):
    def setUp(self):
        super().setUp()
        self.ready(1)
        self.project = radar.Project(name="p", repo=self.repo, github="o/r")

    def admit(self, reading, *, ci=lambda branch: None, project=None):
        reader = mock.Mock(return_value=reading)
        result = merge_queue.enter(None, 1, repo=self.repo, ci=ci,
                                   project=project or self.project, commit_ci=reader)
        reader.assert_called_once_with(self.tip(1))
        return result

    def test_no_pr_uses_green_frozen_commit_ci_without_ci_required(self):
        self.assertFalse(self.project.ci_required)
        row = self.admit({"runs": 1, "green": True, "pending": [], "failed": [],
                          "statuses": {"lot/tests": "success"}})
        self.assertEqual(row["state"], "queued")
        self.assertEqual(row["frozen_sha"], self.tip(1))

    def test_absent_running_red_or_missing_commit_ci_refuses(self):
        for reading in ({"runs": 0, "green": False, "pending": [], "failed": []},
                        {"runs": 1, "green": False, "pending": ["lot/tests"], "failed": []},
                        {"runs": 1, "green": False, "pending": [], "failed": ["lot/tests"]},
                        {"runs": 1, "green": False, "pending": [], "failed": [],
                         "missing": ["lot/"]}):
            with self.subTest(reading=reading), self.assertRaises(merge_queue.QueueError):
                self.admit(reading)
        self.assertEqual(self.fake.queue_rows, {})

    def test_unreadable_pr_never_falls_back(self):
        with mock.patch.object(radar, "commit_ci") as reader:
            with self.assertRaisesRegex(merge_queue.QueueError, "cannot read the CI"):
                self.enter(1, ci=mock.Mock(side_effect=plan_github.GithubError("offline")))
        reader.assert_not_called()

    def test_a_red_or_moved_pr_never_falls_back(self):
        for status in ({"number": 7, "head": self.tip(1), "green": False},
                       {"number": 7, "head": self.tip(2), "green": True}):
            reader = mock.Mock()
            with self.subTest(status=status), self.assertRaises(merge_queue.QueueError):
                merge_queue.enter(None, 1, repo=self.repo, ci=lambda branch: status,
                                  project=self.project, commit_ci=reader)
            reader.assert_not_called()

    def test_default_commit_reader_keeps_branch_and_required_statuses(self):
        project = dataclasses.replace(self.project, required_statuses=(("lot/*", "lot/"),))
        reading = {"runs": 1, "green": True, "pending": [], "failed": []}
        with mock.patch.object(radar, "commit_ci", return_value=reading) as reader:
            merge_queue.enter(None, 1, repo=self.repo, ci=lambda branch: None, project=project)
        reader.assert_called_once_with(project, self.tip(1), branch="lot/1-a")

    def test_due_parts_are_checked_even_without_ci_required(self):
        project = dataclasses.replace(self.project, ci_parts=object())
        reading = {"runs": 1, "green": True, "pending": [], "failed": [],
                   "statuses": {"lot/tests": "success"}}
        with mock.patch.object(merge_queue, "_parts_reader", return_value=("lot/contract",)) \
                as parts:
            with self.assertRaisesRegex(merge_queue.QueueError, "lot/contract"):
                self.admit(reading, project=project)
        parts.assert_called_once()
        self.assertEqual(self.fake.queue_rows, {})

    def test_unreadable_commit_ci_refuses(self):
        with self.assertRaisesRegex(merge_queue.QueueError, "unreadable"):
            merge_queue.enter(None, 1, repo=self.repo, ci=lambda branch: None,
                              project=self.project,
                              commit_ci=mock.Mock(side_effect=plan_github.GithubError("offline")))
        self.assertEqual(self.fake.queue_rows, {})
