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
from ameesh import github_watch as watch, fil, mail, notify, plan_github, registry, storage
from .support import PgTestCase

SUB = watch.Subscription("app", "acme/app", "watcher", 900)
NOW = 1800000000


def issue(n=1, **kw):
    return dict(id=n, number=n, title="A request", body="Please fix export", updated_at=watch.iso(NOW - 1000), **kw)


def comment(n=10, body="More information"):
    return dict(id=n, issue_url="https://api.github.com/repos/acme/app/issues/1", body=body,
                updated_at=watch.iso(NOW - 1000))


class Api:
    def __init__(self, issues=(), comments=()):
        self.issues, self.comments = list(issues), list(comments)
        self.calls = []

    def get(self, path, **kw):
        self.calls.append(path)
        bits = urlsplit(path)
        rows = self.comments if bits.path.endswith("/comments") else self.issues
        page = int(parse_qs(bits.query)["page"][0])
        size = int(parse_qs(bits.query)["per_page"][0])
        return rows[(page - 1) * size:page * size]


class UpdatedApi(Api):
    def get(self, path, **kw):
        self.calls.append(path)
        query = parse_qs(urlsplit(path).query)
        source = self.comments if urlsplit(path).path.endswith("/comments") else self.issues
        eligible = sorted([r for r in source if r["updated_at"] > query["since"][0]],
                          key=lambda r: r["updated_at"])
        page, size = int(query["page"][0]), int(query["per_page"][0])
        return eligible[(page - 1) * size:page * size]


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
        first, scan, until = watch.collect_batch(api, SUB, NOW - 3600, NOW, None)
        events = first
        while scan is not None:
            rest, scan, _ = watch.collect_batch(api, SUB, NOW - 3600, NOW, scan)
            events += rest
        self.assertEqual(len(events), 203)
        self.assertEqual(len(api.calls), 6)
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

    def test_large_feed_is_bounded_and_resumes_past_the_old_page_limit(self):
        api = Api([issue(n) for n in range(1, 10002)])
        scan = dict(since=NOW - 3600, until=NOW, kind="issue", page=100)
        # Legacy offset without a boundary must be replayed safely.
        events, scan, until = watch.collect_batch(api, SUB, NOW, NOW + 500, scan)
        self.assertEqual(events[0]["object_id"], 1)
        self.assertEqual(until, NOW)
        for _ in range(101):
            api.calls.clear()
            more, scan, _ = watch.collect_batch(api, SUB, NOW, NOW + 500, scan)
            events += more
            self.assertLessEqual(len(api.calls), watch.PAGES_PER_TICK)
        self.assertIsNone(scan)
        self.assertEqual(len(events), 10001)

    def test_updated_order_drift_replays_without_losing_boundary_issue_or_comment(self):
        for kind in ("issue", "comment"):
            with self.subTest(kind=kind):
                rows = [(issue(n) if kind == "issue" else comment(n)) for n in range(1, 301)]
                # Equal updated_at is intentional: timestamp-only keys lose ties.
                for row in rows:
                    row["updated_at"] = watch.iso(NOW - 1000)
                api = Api(issues=rows if kind == "issue" else (),
                          comments=rows if kind == "comment" else ())
                events, scan, _ = watch.collect_batch(api, SUB, NOW - 3600, NOW, None)
                more, scan, _ = watch.collect_batch(api, SUB, NOW, NOW + 1, scan)
                events += more
                self.assertEqual(len(events), 200)
                # Move #1 after 200 rows: the old offset lost #201 permanently.
                source = api.issues if kind == "issue" else api.comments
                moved = source.pop(0)
                moved["updated_at"] = watch.iso(NOW + 1)
                source.append(moved)
                events2, scan, _ = watch.collect_batch(api, SUB, NOW, NOW + 5, scan)
                self.assertEqual(events2, [])
                self.assertEqual(scan["page"], 1)
                self.assertNotIn("boundary", scan)
                for _ in range(10):
                    if scan is None:
                        break
                    api.calls.clear()
                    batch, scan, _ = watch.collect_batch(api, SUB, NOW, NOW + 10, scan)
                    self.assertLessEqual(len(api.calls), watch.PAGES_PER_TICK)
                    events += batch
                self.assertIsNone(scan)
                self.assertEqual({e["object_id"] for e in events}, set(range(1, 301)))

    def test_drift_during_next_page_read_is_detected_before_acceptance(self):
        api = Api([issue(n) for n in range(1, 301)])
        _, scan, _ = watch.collect_batch(api, SUB, NOW - 3600, NOW, None)
        get = api.get

        def mutate(path, **kw):
            if "page=2" in path:
                api.issues.append(api.issues.pop(0))
            return get(path, **kw)

        with mock.patch.object(api, "get", side_effect=mutate):
            events, scan, _ = watch.collect_batch(api, SUB, NOW, NOW + 5, scan)
        self.assertEqual(events, [])
        self.assertEqual(scan["page"], 1)

    def test_continuous_updates_finish_fixed_window_with_key_progress(self):
        for kind in ("issue", "comment"):
            with self.subTest(kind=kind):
                rows = [(issue(n) if kind == "issue" else comment(n)) for n in range(1, 601)]
                for n, row in enumerate(rows):
                    row["updated_at"] = watch.iso(NOW - 1000 + n)

                api = UpdatedApi(issues=rows if kind == "issue" else (),
                              comments=rows if kind == "comment" else ())
                scan, events = None, []
                for tick in range(20):
                    api.calls.clear()
                    batch, scan, until = watch.collect_batch(api, SUB, NOW - 3600, NOW + tick, scan)
                    self.assertLessEqual(len(api.calls), watch.PAGES_PER_TICK)
                    events += batch
                    if scan is None:
                        break
                    # Every pass moves a previously read old row to the live tail.
                    source = api.issues if kind == "issue" else api.comments
                    source[tick]["updated_at"] = watch.iso(NOW + tick + 1)
                    # New live entries cannot indefinitely extend this fixed window.
                    for n in range(200):
                        row = issue(10000 + tick * 200 + n) if kind == "issue" else comment(10000 + tick * 200 + n)
                        row["updated_at"] = watch.iso(NOW + tick + 1)
                        source.append(row)
                self.assertIsNone(scan, "continuous updates must not restart the full scan forever")
                self.assertEqual(until, NOW)
                self.assertTrue(set(range(1, 601)) <= {e["object_id"] for e in events})
                self.assertLess(tick, 10)

    def test_key_progress_keeps_timestamp_ties_and_original_comment_window(self):
        rows = [issue(n) for n in range(1, 401)]
        for n, row in enumerate(rows):
            row["updated_at"] = watch.iso(NOW - (1000 if n < 50 else 500 if n < 350 else 100))
        api = UpdatedApi(issues=rows, comments=[comment()])
        scan, events = None, []
        for _ in range(20):
            batch, scan, _ = watch.collect_batch(api, SUB, NOW - 3600, NOW, scan)
            events += batch
            if scan is None:
                break
        self.assertIsNone(scan)
        self.assertEqual({e["object_id"] for e in events if e["kind"] == "issue"}, set(range(1, 401)))
        self.assertEqual([e["object_id"] for e in events if e["kind"] == "comment"], [10])

    def test_window_defers_live_entries_to_next_scan(self):
        api = UpdatedApi(issues=[dict(issue(), updated_at=watch.iso(NOW + 1))])
        events, done, until = watch.collect_batch(api, SUB, NOW - 3600, NOW, None)
        self.assertEqual(events, [])
        self.assertIsNone(done)
        self.assertEqual(until, NOW)
        events, done, _ = watch.collect_batch(api, SUB, NOW - watch.OVERLAP, NOW + 2, None)
        self.assertEqual([e["object_id"] for e in events], [1])
        self.assertIsNone(done)

    def test_collection_bounds_every_excerpt_and_hashes_the_complete_content(self):
        body = "Long request. " * 5000
        title = "Long title " * 100
        original = dict(issue(), body=body, title=title)
        edited = dict(original, body=body + "New requirement at the end")
        events = watch.collect(Api([original, edited], [comment(body=body)]), SUB, NOW)
        self.assertEqual(len(events), 3)
        for event in events:
            self.assertLessEqual(len(event["body"]), watch.MAX_BODY)
            self.assertLessEqual(len(event["title"]), watch.MAX_TITLE)
            self.assertTrue(event["truncated"])
            text = watch.instruction(SUB, event, [])
            self.assertIn("external content truncated", text)
            self.assertIn("before processing this input", text)
            self.assertIn(event["url"], text)
        self.assertNotEqual(events[0]["digest"], events[1]["digest"])

    def test_exact_limits_are_complete_and_each_overflow_is_explicit(self):
        for title_extra, body_extra in ((0, 0), (1, 0), (0, 1)):
            with self.subTest(title_extra=title_extra, body_extra=body_extra):
                row = dict(issue(), title="t" * (watch.MAX_TITLE + title_extra),
                           body="b" * (watch.MAX_BODY + body_extra))
                event = watch.collect(Api([row]), SUB, NOW)[0]
                self.assertEqual(event["truncated"], bool(title_extra or body_extra))
                self.assertEqual("external content truncated" in
                                 watch.instruction(SUB, event, []), event["truncated"])

    def test_large_bodies_and_volume_stay_within_one_batch(self):
        row = dict(issue(), body="Readable request. " * 4096)
        api = Api([dict(row, id=n, number=n) for n in range(1, 1001)])
        events, scan, until = watch.collect_batch(api, SUB, NOW - 3600, NOW, None)
        self.assertEqual(len(events), 100)
        self.assertEqual(len(api.calls), 1)
        self.assertEqual(scan["page"], 2)
        self.assertLessEqual(sum(len(e["body"]) + len(e["title"]) for e in events),
                             200 * (watch.MAX_BODY + watch.MAX_TITLE))
        self.assertTrue(all(e["truncated"] for e in events))
        fallback = watch.instruction(SUB, events[0], [], quote=False)
        self.assertIn("external content truncated", fallback)
        self.assertIn("before processing this input", fallback)

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
        self.db.execute("TRUNCATE github_watch_cursors, github_watch_events, github_watch_scans")
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

    def test_committed_mail_is_written_and_indexed_in_the_thread(self):
        self.watcher(Api([issue()], [comment()])).tick(self.db, NOW)
        messages = self.messages()
        self.assertEqual(len(messages), 2)
        transport = fil.transport_for(self.db.cfg)
        entries = [entry for thread in transport.threads()
                   for entry in transport.read(thread)
                   if "GitHub watch" in entry.text]
        self.assertEqual(len(entries), 2)
        self.assertEqual({mid for entry in entries for mid in entry.meta["ids"]},
                         {int(message["id"]) for message in messages})
        indexed = storage.of(self.db).threads.indexed()
        self.assertTrue(indexed)

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

    def test_unreadable_external_body_is_delivered_without_excerpt(self):
        api = Api([dict(issue(), body="Please inspect " + "A" * 300)])
        self.assertEqual(self.watcher(api).tick(self.db, NOW)[0]["events"], 1)
        messages = self.messages()
        self.assertEqual(len(messages), 1)
        self.assertIn("not reproduced", messages[0]["body"])
        self.assertIn("acme/app#1", messages[0]["body"])
        self.assertEqual(storage.of(self.db).github_watch.cursor("app", "acme/app"), NOW)
        self.watcher(api).tick(self.db, NOW + 901)
        self.assertEqual(len(self.messages()), 1)

    def test_edited_tail_delivers_a_new_explicitly_truncated_message(self):
        body = "Please inspect the export. " * 1000
        api = Api(comments=[comment(body=body)])
        self.watcher(api).tick(self.db, NOW)
        api.comments[0]["body"] += "Additional requirement beyond the excerpt."
        self.watcher(api).tick(self.db, NOW + 901)
        self.assertEqual(len(self.messages()), 2)
        for message in self.messages():
            self.assertIn("external content truncated", message["body"])
            self.assertIn("https://github.com/acme/app/issues/1#issuecomment-10", message["body"])
        self.assertEqual(storage.of(self.db).github_watch.cursor("app", "acme/app"), NOW + 901)

    def test_partial_scan_survives_restart_without_duplicate_or_skipped_input(self):
        api = Api(comments=[comment(n) for n in range(10, 15)])
        with mock.patch.object(watch, "PAGE_SIZE", 2):
            self.watcher(api).tick(self.db, NOW)
            self.assertEqual(len(self.messages()), 2)
            progress = storage.of(self.db).github_watch.scan("app", "acme/app")
            self.assertEqual((progress["kind"], progress["page"]), ("comment", 2))
            self.assertLess(storage.of(self.db).github_watch.cursor("app", "acme/app"), NOW)
            self.watcher(api).tick(self.db, NOW + 901)
            self.watcher(api).tick(self.db, NOW + 902)
        self.assertEqual(len(self.messages()), 5)
        self.assertIsNone(storage.of(self.db).github_watch.scan("app", "acme/app"))
        self.assertEqual(storage.of(self.db).github_watch.cursor("app", "acme/app"), NOW)
        self.assertIn("page=2", api.calls[2])

    def test_drift_checkpoint_replays_without_duplicate_mail_or_early_advance(self):
        api = Api([issue(n) for n in range(1, 7)])
        watcher = self.watcher(api)
        with mock.patch.object(watch, "PAGE_SIZE", 2):
            watcher.tick(self.db, NOW)
            watcher.tick(self.db, NOW + 1)
            self.assertEqual(len(self.messages()), 4)
            api.issues.append(api.issues.pop(0))
            watcher.tick(self.db, NOW + 2)
            self.assertEqual(len(self.messages()), 4)
            self.assertEqual(storage.of(self.db).github_watch.scan("app", "acme/app")["page"], 1)
            self.assertLess(storage.of(self.db).github_watch.cursor("app", "acme/app"), NOW)
            for delta in range(3, 8):
                watcher.tick(self.db, NOW + delta)
        self.assertEqual(len(self.messages()), 6)
        self.assertIsNone(storage.of(self.db).github_watch.scan("app", "acme/app"))
        self.assertEqual(storage.of(self.db).github_watch.cursor("app", "acme/app"), NOW)

    def test_partial_scan_continues_on_next_tick_even_with_hourly_interval(self):
        api = Api(comments=[comment(n) for n in range(10, 15)])
        watcher = watch.Watcher(self.cfg, api=api,
                                declarations=[dataclasses.replace(SUB, interval=3600)])
        with mock.patch.object(watch, "PAGE_SIZE", 2):
            for delta, expected in ((0, 2), (1, 4), (2, 5)):
                watcher.tick(self.db, NOW + delta)
                self.assertEqual(len(self.messages()), expected)
        self.assertIsNone(storage.of(self.db).github_watch.scan("app", "acme/app"))

    def test_failed_partial_batch_retains_previous_checkpoint(self):
        api = Api(comments=[comment(n) for n in range(10, 15)])
        with mock.patch.object(watch, "PAGE_SIZE", 2):
            self.watcher(api).tick(self.db, NOW)
            progress = storage.of(self.db).github_watch.scan("app", "acme/app")
            with mock.patch.object(mail, "send", side_effect=RuntimeError("mailbox failed")):
                self.assertEqual(self.watcher(api).tick(self.db, NOW + 901), [])
            self.assertEqual(storage.of(self.db).github_watch.scan("app", "acme/app"), progress)
            self.assertEqual(len(self.messages()), 2)
            self.watcher(api).tick(self.db, NOW + 1802)
            self.watcher(api).tick(self.db, NOW + 1803)
        self.assertEqual(len(self.messages()), 5)

    def test_stale_partial_batch_cannot_rewind_the_checkpoint(self):
        api = Api(comments=[comment(n) for n in range(10, 15)])
        with mock.patch.object(watch, "PAGE_SIZE", 2):
            events, progress, until = watch.collect_batch(api, SUB, NOW - 3600, NOW, None)
            self.assertEqual(watch.deliver(self.db, SUB, events, until, [], self.cfg.host,
                             partial=True, scan_after=progress), 2)
            self.assertEqual(watch.deliver(self.db, SUB, events, until, [], self.cfg.host,
                             partial=True, scan_after=progress), 0)
        self.assertEqual(storage.of(self.db).github_watch.scan("app", "acme/app"), progress)
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
