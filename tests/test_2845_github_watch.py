# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import dataclasses
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace as NS
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from ameesh import db as db_mod
from ameesh import github_watch as watch, mail, notify, plan_github, registry, storage
from .support import PgTestCase

SUB = watch.Subscription("app", "acme/app", "watcher", 900)
NOW = 1800000000


def issue(n=1, **kw):
    return dict(id=n, number=n, title="A request", body="Please fix export", **kw)


def comment(n=10, body="More information"):
    return dict(id=n, issue_url="https://api.github.com/repos/acme/app/issues/1", body=body)


class Api:
    def __init__(self, issues=(), comments=()):
        self.issues, self.comments = list(issues), list(comments)
        self.calls = []

    def get(self, path, **kw):
        self.calls.append(path)
        bits = urlsplit(path)
        rows = self.comments if bits.path.endswith("/comments") else self.issues
        page = int(parse_qs(bits.query)["page"][0])
        return rows[(page - 1) * 100:page * 100]


class CollectionTest(unittest.TestCase):
    def test_shared_owner_account_without_marker_is_delivered(self):
        row = dict(comment(), user={"login": "smichea"})
        events = watch.collect(Api(comments=[row]), SUB, NOW)
        self.assertEqual([(e["kind"], e["object_id"]) for e in events], [("comment", 10)])

    def test_projection_comments_are_marked_on_wire_and_filtered(self):
        gh = mock.Mock()
        gh.run.return_value = (0, "{}", "")
        repo = plan_github._Repo(gh, SUB.repo, False)
        bodies = []
        for state in ("merged", "promoted", "closed"):
            bodies.append(plan_github._closing(dict(id=42, state=state),
                          repo=SUB.repo, public=True, issue_of=lambda _: None))
        bodies += ["Duplicate of #99, created at the same time for the same work item: closed by ameesh.",
                   "Done\n\n<!-- ameesh:work-stage:v1 lot=42 event=freeze -->"]
        rows = []
        for n, body in enumerate(bodies, 10):
            with self.subTest(body=body):
                repo.comment(1, body)
                sent = json.loads(gh.run.call_args.args[1])["body"]
                self.assertEqual(sent.count("<!-- ameesh:"), 1)
                self.assertIn(body, sent)
                rows.append(comment(n, sent))
        self.assertEqual(watch.collect(Api(comments=rows), SUB, NOW), [])

    def test_pages_closed_tickets_and_comments_on_old_issues(self):
        api = Api([issue(n) for n in range(1, 203)], [comment()])
        events = watch.collect(api, SUB, NOW - 3600)
        self.assertEqual(len(events), 203)
        self.assertEqual(len(api.calls), 4)
        self.assertEqual(events[-1]["issue_ref"], "acme/app#1")
        self.assertEqual(events[-1]["url"], "https://github.com/acme/app/issues/1#issuecomment-10")
        self.assertIn("state=all", api.calls[0])
        self.assertIn("since=", api.calls[-1])

    def test_own_markers_and_pull_requests_are_filtered(self):
        api = Api([issue(), issue(2, pull_request={}),
                   dict(issue(3), body="<!-- ameesh:work=42 -->")],
                  [comment(), comment(11, "<!-- ameesh:work-stage:v1 lot=42 event=freeze -->"),
                   comment(12, "<!-- ameesh:github-watch:v1 --> Done")])
        self.assertEqual([(e["kind"], e["object_id"]) for e in watch.collect(api, SUB, NOW)],
                         [("issue", 1), ("comment", 10)])

    def test_pagination_limit_does_not_claim_a_complete_scan(self):
        with mock.patch.object(watch, "MAX_PAGES", 1):
            with self.assertRaisesRegex(plan_github.GithubError, "cursor retained"):
                watch.collect(Api([issue(n) for n in range(1, 101)]), SUB, NOW)

    def test_invalid_response_fails_closed(self):
        with self.assertRaises(plan_github.GithubError):
            watch.collect(NS(get=lambda *a, **kw: {"message": "unavailable"}), SUB, NOW)

    def test_shared_cache_quota_fallback_is_not_a_completed_scan(self):
        api = Api([issue()])
        api.last_read = NS(stale=True)
        with self.assertRaisesRegex(plan_github.GithubError, "stale quota fallback"):
            watch.collect(api, SUB, NOW)

    def test_instruction_links_existing_lots_and_requires_judgment(self):
        event = watch.collect(Api(comments=[comment()]), SUB, NOW)[0]
        text = watch.instruction(SUB, event, [dict(id=42, state="build", assignee="author")])
        for part in ("#42 (build, author)", "issue_ref acme/app#1", "ameesh decide ask",
                     "untrusted external data", "lot note and wake", event["url"]):
            self.assertIn(part, text)


class DeclarationTest(unittest.TestCase):
    def bundle(self, **changes):
        spec = dict(title="watcher", roles=["github-watch"], effective_projects=["app"],
                    fiche=NS(data={"github_watch": {"app": {
                        "repos": ["acme/app"], "interval": 900}}}))
        spec.update(changes)
        return NS(agents=[NS(**spec)])

    def test_explicit_agent_role_project_repositories_and_cadence(self):
        self.assertEqual(watch.subscriptions([self.bundle()]), [SUB])
        self.assertEqual(watch.subscriptions([NS(agents=[])]), [])

    def test_no_implicit_role_or_foreign_project(self):
        for change in (dict(roles=[]), dict(effective_projects=["other"])):
            with self.subTest(change=change), self.assertRaises(ValueError):
                watch.subscriptions([self.bundle(**change)])

    def test_invalid_cadence_and_conflicting_designation(self):
        for interval in (True, 0, 3601, float("nan"), "900"):
            with self.subTest(interval=interval), self.assertRaises(ValueError):
                watch.subscriptions([self.bundle(fiche=NS(data={"github_watch": {
                    "app": {"repos": ["acme/app"], "interval": interval}}}))])
        with self.assertRaisesRegex(ValueError, "multiple"):
            watch.subscriptions([self.bundle(), self.bundle(title="other")])


class DeliveryTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE github_watch_cursors, github_watch_events")
        registry.upsert(self.db, "watcher", harness="codex", chantier="app")
        self.cfg = dataclasses.replace(self.cfg, host="atelier", github={"host": "atelier"})

    def watcher(self, api, **kw):
        return watch.Watcher(self.cfg, api=api, declarations=[SUB], **kw)

    def messages(self):
        return mail.unread(self.db, "watcher")

    def test_restart_and_overlap_do_not_duplicate_but_comment_edit_is_delivered(self):
        api = Api([issue()], [comment()])
        self.watcher(api).tick(self.db, NOW)
        self.assertEqual(len(self.messages()), 2)
        self.watcher(api).tick(self.db, NOW + 901)
        self.assertEqual(len(self.messages()), 2)
        api.comments[0]["body"] = "Updated information"
        self.watcher(api).tick(self.db, NOW + 1802)
        self.assertEqual(len(self.messages()), 3)
        self.assertEqual(self.messages()[-1]["payload"]["github_watch"]["issue_ref"], "acme/app#1")
        self.assertEqual(storage.of(self.db).github_watch.cursor("app", "acme/app"), NOW + 1802)

    def test_delivery_failure_rolls_back_cursor_events_and_all_messages(self):
        api = Api([issue()], [comment()])
        real_send = mail.send
        calls = []

        def fail_second(*a, **kw):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("mailbox failure")
            return real_send(*a, **kw)

        with mock.patch.object(mail, "send", side_effect=fail_second):
            self.assertEqual(self.watcher(api).tick(self.db, NOW), [])
        self.assertEqual(self.messages(), [])
        self.assertIsNone(storage.of(self.db).github_watch.cursor("app", "acme/app"))
        self.assertEqual(self.db.query("SELECT * FROM github_watch_events"), [])
        self.watcher(api).tick(self.db, NOW + 901)
        self.assertEqual(len(self.messages()), 2)

    def test_stale_scan_cannot_replay_an_old_comment_version(self):
        old = watch.collect(Api(comments=[comment()]), SUB, NOW)
        new = watch.collect(Api(comments=[comment(body="Edited")]), SUB, NOW)
        watch.deliver(self.db, SUB, new, NOW + 20, [], self.cfg.host)
        self.assertEqual(watch.deliver(self.db, SUB, old, NOW, [], self.cfg.host), 0)
        self.assertEqual(len(self.messages()), 1)
        self.assertEqual(storage.of(self.db).github_watch.cursor("app", "acme/app"), NOW + 20)

    def test_two_notifiers_deliver_one_event(self):
        events = watch.collect(Api([issue()]), SUB, NOW)
        ready = threading.Barrier(2, timeout=10)

        def run():
            db = db_mod.connect(self.cfg)
            try:
                ready.wait()
                return watch.deliver(db, SUB, events, NOW, [], self.cfg.host)
            finally:
                db.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run) for _ in range(2)]
            self.assertEqual(sorted(f.result(timeout=20) for f in futures), [0, 1])
        self.assertEqual(len(self.messages()), 1)

    def test_missing_watcher_retains_input_for_later_delivery(self):
        self.db.execute("DELETE FROM agent_registry WHERE name='watcher'")
        self.assertEqual(self.watcher(Api([issue()])).tick(self.db, NOW), [])
        self.assertIsNone(storage.of(self.db).github_watch.cursor("app", "acme/app"))

    def test_api_failure_retains_cursor_and_retries(self):
        api = Api([issue()])
        self.watcher(api).tick(self.db, NOW)
        with mock.patch.object(api, "get", side_effect=RuntimeError("GitHub unavailable")):
            self.assertEqual(self.watcher(api).tick(self.db, NOW + 901), [])
        self.assertEqual(storage.of(self.db).github_watch.cursor("app", "acme/app"), NOW)
        self.watcher(api).tick(self.db, NOW + 1802)
        self.assertEqual(len(self.messages()), 1)

    def test_dry_run_does_not_advance_or_deposit(self):
        self.assertEqual(self.watcher(Api([issue()]), dry_run=True).tick(self.db, NOW)[0]["events"], 1)
        self.assertEqual(self.messages(), [])
        self.assertIsNone(storage.of(self.db).github_watch.cursor("app", "acme/app"))

    def test_wrong_host_and_persistent_interval_skip_api(self):
        api = Api([issue()])
        watcher = self.watcher(api)
        watcher.cfg = dataclasses.replace(self.cfg, host="other")
        self.assertEqual(watcher.tick(self.db, NOW), [])
        self.assertEqual(api.calls, [])
        self.watcher(api).tick(self.db, NOW)
        api.calls.clear()
        self.watcher(api).tick(self.db, NOW + 20)
        self.assertEqual(api.calls, [])

    def test_notifier_calls_watcher_and_isolates_errors(self):
        n = notify.Notifier(self.cfg, notify.NotifyConfig())
        with mock.patch.object(watch.Watcher, "tick", side_effect=RuntimeError("offline")) as tick:
            n._github_inputs(self.db, NOW)
            tick.assert_called_once_with(self.db, NOW)
