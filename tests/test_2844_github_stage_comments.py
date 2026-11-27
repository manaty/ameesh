# SPDX-License-Identifier: AGPL-3.0-only
"""Lifecycle replay, remote receipts, publication boundaries, release evidence."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from ameesh import github_stage_comments as comments, plan_github as pg
from . import canon_staging
from .fake_github import FakeGh

REPO = "acme/app"


class Clock:
    """#8357: an adjustable clock; no test waits on the real one."""

    def __init__(self, now: float = 1000.0):
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


class LimitedGh(FakeGh):
    """Fake GitHub refusing the Nth comment POST (#8357).

    The refused call returns the HTTP response `gh api -i` would print: the
    status, the quota headers (a secondary limit's `Retry-After` included) and
    the JSON body. The refused attempt is counted but not applied.
    """

    def __init__(self, at: int = 3, *, body: str | None = None, status: int = 403,
                 headers: tuple = ("Retry-After: 120",
                                   "X-Ratelimit-Remaining: 4999")):
        super().__init__()
        self.at = int(at)
        self.status = int(status)
        self.body = body or '{"message":"You have exceeded a secondary rate limit."}'
        self.headers = tuple(headers)
        self.comment_posts = 0

    def run(self, argv, stdin=None, timeout=None):
        if (argv[:3] == ["api", "-X", "POST"] and len(argv) > 3
                and argv[3].endswith("/comments")):
            self.comment_posts += 1
            if self.comment_posts == self.at:
                reason = "Too Many Requests" if self.status == 429 else "Forbidden"
                head = "\r\n".join(("HTTP/2.0 %d %s" % (self.status, reason),
                                    *self.headers))
                return 1, head + "\r\n\r\n" + self.body, ""
        return super().run(argv, stdin, timeout)


class RefusingWritesGh(FakeGh):
    """Refuses the first non-comment write with a secondary limit (#8357)."""

    def __init__(self):
        super().__init__()
        self.write_attempts = 0

    def run(self, argv, stdin=None, timeout=None):
        if (len(argv) > 3 and argv[:2] == ["api", "-X"]
                and argv[2] in ("POST", "PATCH", "DELETE", "PUT")
                and not argv[3].endswith("/comments")):
            self.write_attempts += 1
            return 1, ("HTTP/2.0 403 Forbidden\r\n"
                       "Retry-After: 120\r\n"
                       "X-Ratelimit-Remaining: 4999\r\n\r\n"
                       '{"message":"You have exceeded a secondary rate limit."}'), ""
        return super().run(argv, stdin, timeout)


class StageCommentsTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.db = SimpleNamespace(cfg=SimpleNamespace(state_dir=self.folder.name))
        self.item = {"id": 12, "title": "Export", "type": "bug", "app": "app",
                     "state": "build", "issue_ref": REPO + "#7", "priority": 1,
                     "planned_end": "2026-10-22"}
        self.events = [{"id": 1, "state": "build", "created_ts": 1, "note": "private"}]
        self.marks = []
        self.st = SimpleNamespace(work=SimpleNamespace(
            milestones=lambda *_: self.marks, events=lambda *_: self.events))
        self.gh = FakeGh()
        self.gh.repo(REPO)["visibility"] = "private"
        self.gh.repo(REPO)["issues"].append({"id": 9007, "number": 7, "title": "Export",
            "body": "human text", "state": "open", "labels": [], "state_reason": None})
        self.guard = pg.Guard(host_names=("worker-7",))

    def project(self, dry_run=False, writes=None):
        report = {"issues": [{"work_item": 12, "number": 7, "action": "unchanged"}],
                  "comments": [], "withheld": [], "errors": []}
        comments.project(self.db, self.st, pg._Repo(self.gh, REPO, dry_run, writes=writes),
                         report, [(self.item, self.gh.repo(REPO)["issues"][0])],
                         guard=self.guard, refused=set(), project="app", closings={})
        return report

    def make_writes(self, *, comments_per_pass=pg.MAX_COMMENTS_PER_PASS,
                    comments_per_hour=pg.MAX_COMMENTS_PER_HOUR, clock=None):
        """The production write client: shared quota, paced by a fake clock."""
        self.clock = clock or Clock()
        self.sleeps = []

        def sleep(seconds):
            self.sleeps.append(seconds)
            self.clock.advance(seconds)

        reads = pg.SharedReads(self.folder.name, clock=self.clock)
        return pg._Writes(self.gh, reads=reads, clock=self.clock, sleep=sleep,
                          comments_per_pass=comments_per_pass,
                          comments_per_hour=comments_per_hour)

    def use_limited_gh(self, **kwargs):
        """Replace the fake with one refusing the Nth comment POST."""
        gh = LimitedGh(**kwargs)
        gh.repo(REPO)["visibility"] = "private"
        gh.repo(REPO)["issues"].append(dict(self.gh.repo(REPO)["issues"][0]))
        self.gh = gh
        return gh

    def enroll(self):
        """#2861: an existing open mirror starts from its current stage.

        Post that stage with the legacy client (no writes) so the following
        passes, on the same mirror, replay all new candidates."""
        self.project()
        self.gh.reset_calls()
        if hasattr(self.gh, "comment_posts"):
            self.gh.comment_posts = 0

    def bodies(self):
        return self.gh.repo(REPO)["comments"].get("7", [])

    def with_topology(self):
        """Use the canon's real staging sequence (three VMs, server, poste)."""
        return mock.patch.object(comments, "_staging_topology",
                                 return_value=canon_staging.topology())

    def release_report(self, *, version="1.75.0", completed_at=1795732226.0,
                       lots=(), sha="a" * 40, hosts=None, reportes=None,
                       include=None, project="app", result="reconciled"):
        """A release report with a complete staging proof.

        The default shape is the real 1.75.0 one (#8313): the three agent VMs
        active at the commit, the server and the workstation reported absent.
        `lots` are the attestation lots, `include` the cutoff's own list.
        """
        if hosts is None:
            hosts = {host: {"actif": sha} for host in canon_staging.AGENTS}
        if reportes is None:
            reportes = {host: {"raison": "absent at release time"}
                        for host in (canon_staging.SERVER, canon_staging.WORKSTATION)}
        report = {
            "schema": "ameesh-release-cutoff/1", "project": project,
            "result": result, "tag": "v" + version, "version": version,
            "release_sha": sha, "completed_at": completed_at,
            "staging": {"exit": 0, "attestation": {
                "schema": "ameesh-staging-actif/1", "sha": sha,
                "hotes": hosts, "reportes": reportes,
                "lots": [str(lot) for lot in lots]}},
        }
        if include is not None:
            report["included"] = include
        return report

    def write_reports(self, *reports):
        folder = Path(self.folder.name) / "release" / "reports"
        folder.mkdir(parents=True, exist_ok=True)
        for number, report in enumerate(reports):
            (folder / ("%02d.json" % number)).write_text(json.dumps(report))

    def receipts(self, *reports, project="app"):
        self.write_reports(*reports)
        with self.with_topology():
            return comments.release_receipts(self.db.cfg, project)

    def test_release_receipts_prove_the_canon_staging_sequence(self):
        """#8313 witness: the real 1.75.0 shape proves the release.

        Three agent VMs are active and the server and the workstation are
        reported absent, as the canon allows (§14); the old fixed count of
        four hosts refused exactly this report, so no requester got the
        "Delivered in release 1.75.0" confirmation.
        """
        report = self.release_report(lots=(8264, 8296))
        receipts = self.receipts(report)
        self.assertEqual(sorted(receipts), [8264, 8296])
        self.assertEqual(receipts[8296],
                         [("1.75.0", "2026-11-26", 1795732226.0)])

    def test_release_receipts_accept_a_full_sequence(self):
        hosts = {host: {"actif": "a" * 40} for host in canon_staging.HOSTS}
        report = self.release_report(hosts=hosts, reportes={}, lots=(12,))
        self.assertEqual(sorted(self.receipts(report)), [12])

    def test_release_receipts_refuse_an_incomplete_sequence(self):
        report = self.release_report(lots=(12,))
        del report["staging"]["attestation"]["reportes"]
        self.assertEqual(self.receipts(report), {})

    def test_release_receipts_refuse_a_reported_agent_vm(self):
        # The union covers the sequence, but an agent VM is never reportable.
        agents = canon_staging.AGENTS
        report = self.release_report(
            lots=(12,),
            hosts={agents[1]: {"actif": "a" * 40},
                   agents[2]: {"actif": "a" * 40},
                   canon_staging.WORKSTATION: {"actif": "a" * 40}},
            reportes={agents[0]: {"raison": "absent"},
                      canon_staging.SERVER: {"raison": "absent"}})
        self.assertEqual(self.receipts(report), {})

    def test_release_receipts_refuse_a_commit_mismatch_or_missing_reason(self):
        bad_sha = self.release_report(lots=(12,))
        bad_sha["staging"]["attestation"]["hotes"][canon_staging.AGENTS[0]]["actif"] = "b" * 40
        self.assertEqual(self.receipts(bad_sha), {})
        no_reason = self.release_report(lots=(12,))
        no_reason["staging"]["attestation"]["reportes"][canon_staging.SERVER] = {}
        self.assertEqual(self.receipts(no_reason), {})

    def test_release_receipts_need_a_canon_sequence(self):
        # #7662: no readable canon declares no host, so nothing is proven.
        self.write_reports(self.release_report(lots=(12,)))
        self.assertEqual(comments.release_receipts(self.db.cfg, "app"), {})

    def test_the_sequence_comes_from_the_default_canon_only(self):
        other = canon_staging.fake_canon()
        other.hosts = list(other.hosts) + [SimpleNamespace(
            title="other-host",
            staging=canon_staging.canon.HostStaging(order=60, kind="agents"))]
        with mock.patch.object(comments.canon_mod, "load_configured",
                               return_value=[canon_staging.fake_canon(), other]):
            topology = comments._staging_topology(self.db.cfg)
        self.assertEqual(topology.hosts, canon_staging.HOSTS)
        self.assertEqual(topology.reportable,
                         {canon_staging.SERVER, canon_staging.WORKSTATION})

    def test_catch_up_keeps_one_delivery_bounded_by_since(self):
        """#2874/#8313: one delivery per lot, at its first release after _since."""
        first = self.release_report(version="1.70.0", completed_at=1795430054.0,
                                    lots=(1, 2))
        second = self.release_report(version="1.71.0", completed_at=1795433654.0,
                                     lots=(2, 3))
        receipts = self.receipts(first, second)
        self.assertEqual(comments._current_receipts(receipts[1], 0),
                         [("1.70.0", "2026-11-23")])
        self.assertEqual(comments._current_receipts(receipts[2], 0),
                         [("1.70.0", "2026-11-23")])
        self.assertEqual(comments._current_receipts(receipts[3], 0),
                         [("1.71.0", "2026-11-23")])
        # A cutoff between the two releases drops the earlier one.
        self.assertEqual(comments._current_receipts(receipts[1], 1795433000), [])
        self.assertEqual(comments._current_receipts(receipts[2], 1795433000),
                         [("1.71.0", "2026-11-23")])

    def test_replay_between_passes_order_and_idempotence(self):
        self.marks = [dict(id=4, kind="merged", at_ts=5), dict(id=3, kind="frozen", at_ts=3)]
        self.events = [dict(id=8, state="build", created_ts=4),
                       dict(id=7, state="blocked", created_ts=2,
                            note="internal\nPublic status: Please confirm the CSV contract on this ticket."),
                       *self.events]
        marks, events = self.marks, self.events
        self.marks, self.events = [], self.events[-1:]
        self.assertEqual([r["event"] for r in self.project()["comments"]], ["accepted"])
        self.marks, self.events = marks, events
        self.item["state"] = "merged"
        report = self.project()
        self.assertEqual([r["event"] for r in report["comments"]],
                         ["event-7", "frozen", "milestone-4"])
        text = "\n".join(self.bodies())
        for phrase in ("Priority: P1", "2026-10-22", "Please confirm the CSV contract", "Merged;"):
            self.assertIn(phrase, text)
        self.assertNotIn("internal", text)
        self.gh.reset_calls()
        self.assertEqual(self.project()["comments"], [])
        self.assertEqual(self.gh.writes(), [])

    def test_preexisting_open_ticket_gets_current_stage_and_one_freeze(self):
        # #2861: enrolling an open mirror that predates the feature posts no
        # history, and re-freezes never repeat "Frozen for review."
        self.marks = [dict(id=n, kind="frozen", at_ts=n) for n in (6, 5, 4, 3)]
        self.events = [dict(id=7, state="blocked", created_ts=5,
                            note="Public status: Please confirm."), *self.events]
        self.assertEqual([r["event"] for r in self.project()["comments"]], ["frozen"])
        self.assertEqual(len(self.bodies()), 1)
        self.marks = [dict(id=8, kind="frozen", at_ts=8), *self.marks]
        self.assertEqual(self.project()["comments"], [])
        self.marks = [dict(id=9, kind="merged", at_ts=9), *self.marks]
        self.item["state"] = "merged"
        self.assertEqual([r["event"] for r in self.project()["comments"]], ["milestone-9"])

    def test_post_failure_and_lost_ack_reconciled_without_issue_update(self):
        self.project()
        self.marks = [dict(id=3, kind="frozen", at_ts=3)]
        self.gh.fail = lambda argv: "-X" in argv and "POST" in argv
        self.assertEqual(len(self.project()["errors"]), 1)
        self.gh.fail = None
        original = self.gh.run

        def lost_ack(argv, stdin=None):
            result = original(argv, stdin)
            if "-X" in argv and "POST" in argv:
                return 1, "", "response lost"
            return result

        with mock.patch.object(self.gh, "run", side_effect=lost_ack):
            self.assertEqual(len(self.project()["errors"]), 1)
        self.assertEqual(len(self.bodies()), 2)
        self.gh.reset_calls()
        self.assertEqual(self.project()["errors"], [])
        self.assertEqual(self.gh.writes(), [])

    def test_secondary_limit_stops_the_pass_and_is_remembered(self):
        # #8357: the 3rd comment is refused; the pass stops, the first two
        # stay, the fourth is never attempted. The window and its Retry-After
        # are remembered, so the next pass sends nothing until it expires.
        gh = self.use_limited_gh(at=3)
        self.enroll()                                # accepted, once
        self.events = [dict(id=n, state="blocked" if n % 2 else "build", created_ts=n)
                       for n in range(1, 6)]
        report = self.project(writes=self.make_writes())
        self.assertEqual(gh.comment_posts, 3)
        self.assertEqual([r["event"] for r in report["comments"]],
                         ["event-1", "event-3"])
        self.assertEqual(len(self.bodies()), 3)      # accepted + two events
        self.assertEqual(len(report["errors"]), 1)
        self.assertEqual(report["errors"][0]["action"], "stage-comment")
        self.assertIn("rate limit", report["errors"][0]["detail"].lower())
        self.assertIsNotNone(pg.SharedReads(self.folder.name, clock=self.clock).blocked())

        gh.comment_posts = 0
        gh.reset_calls()
        blocked = self.project(writes=self.make_writes())
        self.assertEqual(gh.comment_posts, 0)       # refused without a POST
        self.assertEqual(len(blocked["errors"]), 1)
        self.assertEqual(len(self.bodies()), 3)     # no duplicate

        # After Retry-After, the remaining marker replays, with no duplicate.
        resumed = self.project(writes=self.make_writes(clock=Clock(1200.0)))
        self.assertEqual([r["event"] for r in resumed["comments"]], ["event-5"])
        self.assertEqual(len(self.bodies()), 4)
        self.assertEqual(len(set(self.bodies())), 4)
        self.gh.reset_calls()
        self.assertEqual(self.project(writes=self.make_writes(clock=Clock(1300.0)))["comments"], [])
        self.assertEqual(self.gh.writes(), [])

    def test_limit_stops_before_the_next_lot(self):
        # #8357: the quota ends the PASS, not just the current ticket. The
        # next lot is not attempted in the same burst.
        gh = self.use_limited_gh(at=2)
        self.enroll()
        issue8 = {"id": 9008, "number": 8, "title": "Second", "body": "human",
                  "state": "open", "labels": [], "state_reason": None}
        self.gh.repo(REPO)["issues"].append(issue8)
        other = dict(self.item, id=13, title="Second", issue_ref=REPO + "#8")
        self.events = [dict(id=n, state="blocked" if n % 2 else "build", created_ts=n)
                       for n in (1, 2, 3)]
        report = {"issues": [{"work_item": 12, "number": 7, "action": "unchanged"},
                             {"work_item": 13, "number": 8, "action": "unchanged"}],
                  "comments": [], "withheld": [], "errors": []}
        comments.project(
            self.db, self.st, pg._Repo(self.gh, REPO, False, writes=self.make_writes()),
            report, [(self.item, self.gh.repo(REPO)["issues"][0]), (other, issue8)],
            guard=self.guard, refused=set(), project="app", closings={})
        self.assertEqual(gh.comment_posts, 2)        # event-1, then lot 13's accepted
        self.assertEqual([(r["work_item"], r["event"]) for r in report["comments"]],
                         [(12, "event-1")])
        self.assertEqual(self.gh.repo(REPO)["comments"].get("8", []), [])
        self.assertEqual(len(report["errors"]), 1)

    def test_primary_limit_also_stops_the_pass(self):
        # #8357: a primary refusal (remaining 0, reset) stops like a secondary
        # one; the pass does not try the following lots.
        gh = self.use_limited_gh(
            at=2, body='{"message":"API rate limit exceeded"}',
            headers=("X-Ratelimit-Remaining: 0", "X-Ratelimit-Reset: 1060"))
        self.enroll()
        self.events = [dict(id=n, state="blocked" if n % 2 else "build", created_ts=n)
                       for n in range(1, 5)]
        report = self.project(writes=self.make_writes())
        self.assertEqual(gh.comment_posts, 2)
        self.assertEqual(len(self.bodies()), 2)      # accepted + event-1
        self.assertEqual(len(report["errors"]), 1)
        self.assertIn("rate limit", report["errors"][0]["detail"].lower())
        self.assertIsNotNone(pg.SharedReads(self.folder.name, clock=self.clock).blocked())

    def test_generic_429_without_a_marker_stops_and_is_remembered(self):
        # #8357: GitHub sometimes answers 429 "Too Many Requests" without a
        # body marker or Retry-After. It is still a limit: stop, remember a
        # default window, and do not send the next lot.
        gh = self.use_limited_gh(at=2, status=429,
                                 body='{"message":"Too Many Requests"}', headers=())
        self.enroll()
        self.events = [dict(id=n, state="blocked" if n % 2 else "build", created_ts=n)
                       for n in range(1, 5)]
        report = self.project(writes=self.make_writes())
        self.assertEqual(gh.comment_posts, 2)
        self.assertEqual(len(self.bodies()), 2)      # accepted + event-1
        self.assertEqual(len(report["errors"]), 1)
        self.assertIn("rate limit", report["errors"][0]["detail"].lower())
        self.assertIsNotNone(pg.SharedReads(self.folder.name, clock=self.clock).blocked())

    def test_hourly_credit_stops_then_resumes_at_the_next_window(self):
        # #8357: 20 per pass at a 90 s cadence would still allow 800/h. A
        # persistent hourly credit keeps the projection well below GitHub's
        # ~500/h and leaves room for merges, CI and other token writers.
        self.enroll()
        self.events = [dict(id=n, state="blocked" if n % 2 else "build", created_ts=n)
                       for n in range(1, 21)]
        first = self.project(writes=self.make_writes(comments_per_hour=2))
        self.assertEqual(len(first["comments"]), 2)
        self.assertEqual(len(self.bodies()), 3)      # accepted + two
        self.gh.reset_calls()
        second = self.project(writes=self.make_writes(comments_per_hour=2))
        self.assertEqual(second["comments"], [])     # credit spent, no POST
        self.assertEqual(self.gh.calls(), [])        # nor a thread read
        self.assertEqual(len(self.bodies()), 3)
        window = Clock(1000.0 + pg.COMMENT_CREDIT_WINDOW_S + 1)
        third = self.project(writes=self.make_writes(comments_per_hour=2, clock=window))
        self.assertEqual(len(third["comments"]), 2)
        self.assertEqual(len(self.bodies()), 5)

    def test_comment_budget_is_paced_and_resumes_without_duplicates(self):
        # #8357: a catch-up is spread over passes. The mirror is enrolled
        # first (accepted); 25 blocked events follow and this pass posts 20,
        # spaced one second. The five others wait for the next pass.
        self.enroll()
        self.events = [dict(id=n, state="blocked" if n % 2 else "build", created_ts=n)
                       for n in range(1, 51)]
        first = self.project(writes=self.make_writes())
        self.assertEqual(len(first["comments"]), 20)
        self.assertEqual(self.sleeps, [pg.COMMENT_INTERVAL_S] * 19)
        self.assertEqual(self.clock.now, 1000.0 + 19 * pg.COMMENT_INTERVAL_S)
        self.assertEqual(len(self.bodies()), 21)
        second = self.project(writes=self.make_writes())
        self.assertEqual(len(second["comments"]), 5)
        self.assertEqual(self.sleeps, [pg.COMMENT_INTERVAL_S] * 4)
        self.assertEqual(len(self.bodies()), 26)
        self.assertEqual(len(set(self.bodies())), 26)
        third = self.project(writes=self.make_writes())
        self.assertEqual(third["comments"], [])
        self.assertEqual(len(self.bodies()), 26)

    def test_patch_uses_the_shared_window_and_is_refused_immediately(self):
        # #8357: PATCH writes go through the same quota memory as reads; once
        # a secondary limit is known, no further request leaves the process.
        gh = RefusingWritesGh()
        self.gh = gh
        writes = self.make_writes()
        with self.assertRaises(pg.GithubRateLimited):
            writes.write("PATCH", "repos/acme/app/issues/7", {"title": "x"})
        self.assertEqual(gh.write_attempts, 1)
        self.assertIsNotNone(pg.SharedReads(self.folder.name, clock=self.clock).blocked())
        with self.assertRaises(pg.GithubRateLimited):
            writes.write("PATCH", "repos/acme/app/issues/7", {"title": "x"})
        self.assertEqual(gh.write_attempts, 1)

    def test_headerless_secondary_refusal_still_stops_and_is_remembered(self):
        # #8357: some `gh` versions report the refusal on stderr without an
        # HTTP status line. The text still stops the pass, and the default
        # window is remembered so the next pass sends nothing.
        gh = self.gh
        original = gh.run

        def run(argv, stdin=None, timeout=None):
            if argv[:2] == ["api", "-X"]:
                return 1, "", "gh: You have exceeded a secondary rate limit. (HTTP 403)"
            return original(argv, stdin, timeout)

        with mock.patch.object(gh, "run", side_effect=run):
            writes = self.make_writes()
            with self.assertRaises(pg.GithubRateLimited):
                writes.write("POST", "repos/acme/app/issues", {"title": "x"})
        self.assertIsNotNone(pg.SharedReads(self.folder.name, clock=self.clock).blocked())

    def test_paginated_read_failure_never_posts_and_dry_run_is_read_only(self):
        self.assertEqual(len(self.project(dry_run=True)["comments"]), 1)
        self.assertEqual(self.gh.writes(), [])
        reads = [a for a in self.gh.calls() if "--paginate" in a]
        self.assertTrue(any("comments?per_page=100" in " ".join(a) for a in reads))
        self.gh.fail = lambda argv: "--paginate" in argv
        self.assertEqual(len(self.project()["errors"]), 1)
        self.assertEqual(self.bodies(), [])

    def test_private_notes_and_unsafe_opt_in_are_withheld_even_in_private_repo(self):
        for event_id, secret in enumerate(("/home/bob/private", "worker-7", "ghp_" + "a1" * 15), 7):
            with self.subTest(secret=secret[:10]):
                state = "blocked" if event_id == 8 else "waiting_human"
                self.events = [dict(id=event_id, state=state, created_ts=2,
                                    note="private detail\nPublic status: Please use " + secret)]
                report = self.project()
                self.assertTrue(report["withheld"])
                self.assertNotIn(secret, json.dumps(report))
                self.assertNotIn(secret, "\n".join(self.bodies()))
                prefix = "Blocked. " if state == "blocked" else "Awaiting a decision. "
                self.assertIn(prefix + comments.WAIT_DETAIL, self.bodies()[-1])
        self.events = [dict(id=20, state="blocked", created_ts=3, note="private only")]
        self.project()
        self.assertNotIn("private only", "\n".join(self.bodies()))
        self.assertIn("required decision or information", self.bodies()[-1])

    def test_delivery_needs_release_and_matching_activation(self):
        # releases from the epoch on count (#2874: the cutoff is tested below)
        (Path(self.folder.name) / "github-stage-comments.json").write_text('{"_since": 0}')
        self.item["state"] = "promoted"
        self.marks = [dict(id=2, kind="delivered", at_ts=3)]
        self.project()
        self.assertNotIn("Delivered in release", "\n".join(self.bodies()))
        folder = Path(self.folder.name) / "release" / "reports"
        folder.mkdir(parents=True)
        report = self.release_report(version="v1.2.3", completed_at=1792454400,
                                     include=[12], lots=(12,))
        path = folder / "release.json"
        for bad in (dict(report, staging=None), dict(report, project="other"),
                    dict(report, staging={"exit": 0}),
                    dict(report, result="refused-red")):
            path.write_text(json.dumps(bad))
            self.project()
            self.assertNotIn("Delivered in release", "\n".join(self.bodies()))
        bad = copy.deepcopy(report)
        bad["staging"]["attestation"]["sha"] = "b" * 40
        path.write_text(json.dumps(bad))
        self.project()
        self.assertNotIn("Delivered in release", "\n".join(self.bodies()))
        # A reconciled report may get included lots from the activation proof.
        report.pop("included")
        report["result"] = "reconciled"
        path.write_text(json.dumps(report))
        with self.with_topology():
            self.project()
            self.assertIn("Delivered in release v1.2.3 on 2026-10-20", self.bodies()[-1])
            self.gh.reset_calls()
            self.project()
            self.assertEqual(self.gh.writes(), [])
            self.assertEqual(self.gh.calls(), [])

    def test_preexisting_closed_ticket_does_not_backfill_or_read_thread(self):
        self.item["state"] = "closed"
        self.gh.repo(REPO)["issues"][0]["state"] = "closed"
        self.marks = [dict(id=4, kind="merged", at_ts=5), dict(id=3, kind="frozen", at_ts=3)]
        self.st.hosts = SimpleNamespace(current=lambda *_: [])
        self.st.work.issue_feed = lambda *_: [self.item]
        before = copy.deepcopy(self.bodies())
        with mock.patch.object(pg.storage, "of", return_value=self.st):
            for _ in range(2):
                self.gh.reset_calls()
                result = pg.project_lots(self.db, self.gh, "app", REPO)
                self.assertEqual(result["comments"], [])
                self.assertFalse(any("/comments" in " ".join(a) for a in self.gh.calls()))
        self.assertEqual(self.bodies(), before)

    def test_closure_retries_after_post_failure_on_now_closed_ticket(self):
        self.st.hosts = SimpleNamespace(current=lambda *_: [])
        self.st.work.issue_feed = lambda *_: [self.item]
        with mock.patch.object(pg.storage, "of", return_value=self.st):
            # Existing mirror from before enabling stage comments.
            with mock.patch.object(comments, "project"):
                pg.project_lots(self.db, self.gh, "app", REPO)
            self.item.update(state="merged", pr_ref=REPO + "#42")
            self.gh.fail = lambda argv: "POST" in argv and "/comments" in " ".join(argv)
            result = pg.project_lots(self.db, self.gh, "app", REPO)
            self.assertTrue(result["errors"])
            self.assertEqual(self.gh.repo(REPO)["issues"][0]["state"], "closed")
            self.gh.fail = None
            result = pg.project_lots(self.db, self.gh, "app", REPO)
            # #2861: a mirror enrolled late gets its current stage only
            self.assertEqual([r["event"] for r in result["comments"]], ["merged"])
            self.assertIn("Merged by PR #42;", self.bodies()[-1])
            self.gh.reset_calls()
            self.assertEqual(pg.project_lots(self.db, self.gh, "app", REPO)["comments"], [])
            self.assertFalse(any("/comments" in " ".join(a) for a in self.gh.calls()))

    def test_closed_unenrolled_ticket_only_receives_proven_delivery(self):
        self.gh.repo(REPO)["issues"][0]["state"] = "closed"
        self.item["state"] = "promoted"
        with mock.patch.object(comments, "release_receipts", return_value={12: [("v1.2.3", "2026-10-20")]}):
            result = self.project()
            self.assertEqual([r["event"] for r in result["comments"]], ["delivered-v1.2.3"])
            self.gh.reset_calls()
            self.assertEqual(self.project()["comments"], [])
            self.assertEqual(self.gh.calls(), [])

    def test_past_releases_are_not_replayed_and_one_delivery_per_lot(self):
        # #2874: 1.13/1.14/1.15 were replayed as three "Delivered" comments on
        # every mirror the first time stage comments ran.
        self.gh.repo(REPO)["issues"][0]["state"] = "closed"
        self.item["state"] = "promoted"
        (Path(self.folder.name) / "github-stage-comments.json").write_text('{"_since": 1000}')
        old = [("v1.13.0", "2026-10-18", 10.0), ("v1.14.0", "2026-10-19", 20.0)]
        with mock.patch.object(comments, "release_receipts", return_value={12: old}):
            self.assertEqual(self.project()["comments"], [])
        new = old + [("v1.16.0", "2026-10-21", 2000.0), ("v1.17.0", "2026-10-22", 3000.0)]
        with mock.patch.object(comments, "release_receipts", return_value={12: new}):
            self.assertEqual([r["event"] for r in self.project()["comments"]],
                             ["delivered-v1.16.0"])
            self.assertEqual(self.project()["comments"], [])

    def test_first_pass_records_the_cutoff_even_without_comments(self):
        self.gh.repo(REPO)["issues"][0]["state"] = "closed"
        with mock.patch.object(comments, "release_receipts", return_value={}):
            self.project()
        saved = json.loads((Path(self.folder.name) / "github-stage-comments.json").read_text())
        self.assertIsInstance(saved["_since"], float)

    def test_late_earlier_report_never_announces_a_second_delivery(self):
        self.gh.repo(REPO)["issues"][0]["state"] = "closed"
        checkpoint = Path(self.folder.name) / "github-stage-comments.json"
        checkpoint.write_text('{"_since": 1000}')
        newer = [("v1.17.0", "2026-10-22", 3000.0)]
        with mock.patch.object(comments, "release_receipts", return_value={12: newer}):
            self.assertEqual([r["event"] for r in self.project()["comments"]],
                             ["delivered-v1.17.0"])
        earlier = [("v1.16.0", "2026-10-21", 2000.0), *newer]
        with mock.patch.object(comments, "release_receipts", return_value={12: earlier}):
            self.assertEqual(self.project()["comments"], [])
            # Lost local acknowledgement must reconcile any remote delivery.
            checkpoint.write_text('{"_since": 1000}')
            self.assertEqual(self.project()["comments"], [])
        self.assertEqual(len(self.bodies()), 1)

    def test_dry_run_never_enrolls_or_consumes_receipts(self):
        self.project(dry_run=True)
        self.assertFalse((Path(self.folder.name) / "github-stage-comments.json").exists())
        self.assertEqual(len(self.project()["comments"]), 1)
        self.gh.reset_calls()
        self.assertEqual(self.project()["comments"], [])
        self.assertEqual(self.gh.calls(), [])

    def test_project_lots_routes_comment_posts_through_the_quota_client(self):
        # #8357: the production path (Projector.tick -> project_lots) must
        # hand its writes to the quota client, not call `gh api` directly.
        self.st.hosts = SimpleNamespace(current=lambda *_: [])
        self.st.work.issue_feed = lambda *_: [self.item]
        with mock.patch.object(pg.storage, "of", return_value=self.st):
            result = pg.project_lots(self.db, self.gh, "app", REPO)
        self.assertEqual([r["event"] for r in result["comments"]], ["accepted"])
        posts = [c for c in self.gh.writes() if "/comments" in " ".join(c)]
        self.assertEqual(len(posts), 1)
        self.assertIn("-i", posts[0])

    def test_real_projection_calls_comments_when_issue_is_unchanged(self):
        self.st.hosts = SimpleNamespace(current=lambda *_: [])
        self.st.work.issue_feed = lambda *_: [self.item]
        with mock.patch.object(pg.storage, "of", return_value=self.st):
            pg.project_lots(self.db, self.gh, "app", REPO)
            self.gh.reset_calls()
            self.events = [dict(id=7, state="blocked", created_ts=2,
                                note="Public status: Please confirm the contract."), *self.events]
            result = pg.project_lots(self.db, self.gh, "app", REPO)
        self.assertEqual(result["counts"]["unchanged"], 1)
        self.assertEqual(result["comments"][0]["event"], "event-7")
        self.assertTrue(all("PATCH" not in a for a in self.gh.writes()))

    def test_external_issue_ref_comments_created_and_rediscovered_mirror(self):
        self.item["issue_ref"] = "external/requests#42"
        self.gh.repo(REPO)["issues"] = []
        self.st.hosts = SimpleNamespace(current=lambda *_: [])
        self.st.work.issue_feed = lambda *_: [self.item]
        self.marks = [dict(id=3, kind="frozen", at_ts=3)]
        with mock.patch.object(pg.storage, "of", return_value=self.st):
            result = pg.project_lots(self.db, self.gh, "app", REPO)
            self.assertEqual(result["errors"], [])
            self.assertEqual([r["event"] for r in result["comments"]],
                             ["accepted", "frozen"])
            mirror = result["issues"][0]["number"]
            self.assertTrue(all(r["number"] == mirror for r in result["comments"]))
            self.assertEqual(self.item["issue_ref"], "external/requests#42")
            self.events = [dict(id=7, state="blocked", created_ts=4,
                                note="Public status: Please confirm the contract."), *self.events]
            result = pg.project_lots(self.db, self.gh, "app", REPO)
            self.assertEqual(result["errors"], [])
            self.assertEqual(result["counts"]["unchanged"], 1)
            self.assertEqual([(r["number"], r["event"]) for r in result["comments"]],
                             [(mirror, "event-7")])
            self.assertEqual(self.item["issue_ref"], "external/requests#42")
            self.gh.reset_calls()
            self.assertEqual(pg.project_lots(self.db, self.gh, "app", REPO)["comments"], [])
            self.assertEqual(self.gh.writes(), [])
        self.assertEqual(set(self.gh.state["repos"]), {REPO})
