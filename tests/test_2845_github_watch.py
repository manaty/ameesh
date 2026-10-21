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
    return dict(id=n, number=n, title="A request", body="Please fix export",
                html_url="https://github.com/acme/app/issues/%d" % n,
                updated_at=watch.iso(NOW - 1000), **kw)


def comment(n=10, body="More information"):
    return dict(id=n, issue_url="https://api.github.com/repos/acme/app/issues/1",
                html_url="https://github.com/acme/app/issues/1#issuecomment-%d" % n,
                body=body, updated_at=watch.iso(NOW - 1000))


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


class LagApi(Api):
    """GitHub filters and sorts by an index key that can lag behind updated_at."""
    def get(self, path, **kw):
        self.calls.append(path)
        query = parse_qs(urlsplit(path).query)
        source = self.comments if urlsplit(path).path.endswith("/comments") else self.issues
        key = lambda r: r.get("key", r["updated_at"])
        eligible = sorted([r for r in source if key(r) > query["since"][0]], key=key)
        page, size = int(query["page"][0]), int(query["per_page"][0])
        return eligible[(page - 1) * size:page * size]


class CollectionTest(unittest.TestCase):
    def test_shared_owner_account_without_marker_is_delivered(self):
        row = dict(comment(), user={"login": "smichea"})
        events = watch.collect(Api(comments=[row]), SUB, NOW)
        self.assertEqual([(e["kind"], e["object_id"]) for e in events], [("comment", 10)])

    def test_discussion_comment_on_a_pull_request_is_included_with_its_link(self):
        row = comment(12, "Please check this")
        row["issue_url"] = "https://api.github.com/repos/acme/app/issues/7"
        row["html_url"] = "https://github.com/acme/app/pull/7#issuecomment-12"
        events = watch.collect(Api(comments=[row]), SUB, NOW)
        self.assertEqual(events[0]["issue_ref"], "acme/app#7")
        self.assertEqual(events[0]["url"],
                         "https://github.com/acme/app/pull/7#issuecomment-12")

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
                # #2955: the re-read prefix now ends with #201; it is delivered
                # with the page fetched before the check, so nothing is lost.
                self.assertEqual({e["object_id"] for e in events2}, set(range(201, 301)))
                events += events2
                for _ in range(10):
                    if scan is None:
                        break
                    api.calls.clear()
                    batch, scan, _ = watch.collect_batch(api, SUB, NOW, NOW + 10, scan)
                    self.assertLessEqual(len(api.calls), watch.PAGES_PER_TICK)
                    events += batch
                self.assertIsNone(scan)
                self.assertEqual({e["object_id"] for e in events}, set(range(1, 301)))

    def test_drift_during_next_page_read_is_reconciled_without_skipping(self):
        api = Api([issue(n) for n in range(1, 301)])
        _, scan, _ = watch.collect_batch(api, SUB, NOW - 3600, NOW, None)
        get = api.get

        def mutate(path, **kw):
            if "page=2" in path:
                api.issues.append(api.issues.pop(0))
            return get(path, **kw)

        with mock.patch.object(api, "get", side_effect=mutate):
            events, scan, _ = watch.collect_batch(api, SUB, NOW, NOW + 5, scan)
        # #2955: the offset moved while page 2 was read. The re-read page 1
        # now ends with #101, delivered with the already-read page 2
        # (#102..#201), so the moved boundary skips no row.
        self.assertEqual({e["object_id"] for e in events}, set(range(101, 202)))
        self.assertEqual(scan["page"], 3)

    def test_shifted_row_is_delivered_once_with_its_new_version(self):
        # #2955: a row can move from page 2 into page 1 while it is edited
        # between the two reads. The later read (page 1) is the new version;
        # the copy read with page 2 must not be delivered after it.
        with mock.patch.object(watch, "PAGE_SIZE", 2):
            api = Api([issue(n) for n in range(1, 5)])
            _, scan, _ = watch.collect_batch(api, SUB, NOW - 3600, NOW, None)
            get = api.get
            mutated = []

            def mutate(path, **kw):
                if "page=2" in path and not mutated:
                    out = [dict(row) for row in get(path, **kw)]
                    api.issues.pop(0)
                    api.issues[1]["body"] = "Edited"  # #3, now on page 1
                    api.issues.append(dict(issue(1), body="Moved",
                                           updated_at=watch.iso(NOW + 1)))
                    mutated.append(True)
                    return out
                return get(path, **kw)

            with mock.patch.object(api, "get", side_effect=mutate):
                events, _, _ = watch.collect_batch(api, SUB, NOW, NOW + 5, scan)
        self.assertEqual([(e["object_id"], e["body"]) for e in events],
                         [(3, "Edited"), (4, "Please fix export")])

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

    def test_index_lag_on_a_dense_page_tail_cannot_skip_the_next_row(self):
        # #2955: page of 3 keys NOW-250/-200/-195 whose last row carries the
        # updated_at NOW-190 (index lag); seeking to updated_at - 1 skipped the
        # row keyed NOW-194, which was never read again. A dense page cannot be
        # advanced safely by the lag margin: it pages by guarded offset.
        with mock.patch.object(watch, "PAGE_SIZE", 3):
            rows = [issue(n) for n in range(1, 5)]
            for row, key, updated in zip(rows, (-250, -200, -195, -194),
                                         (-250, -200, -190, -194)):
                row["key"] = watch.iso(NOW + key)
                row["updated_at"] = watch.iso(NOW + updated)
            api, scan, events = LagApi(rows, []), None, []
            for _ in range(10):
                batch, scan, _ = watch.collect_batch(api, SUB, NOW - 3600, NOW, scan)
                events += batch
                if scan is None:
                    break
            self.assertIsNone(scan)
            self.assertEqual({e["object_id"] for e in events}, {1, 2, 3, 4})

    def test_dense_page_whose_prefix_moves_does_not_skip_the_lagged_successor(self):
        # #2955 review: the old fallback advanced since to the last row's
        # updated_at - 1 when a dense page's boundary moved. The successor
        # keyed between that key and the timestamp was never read again.
        with mock.patch.object(watch, "PAGE_SIZE", 3):
            rows = [issue(n) for n in range(1, 5)]
            for row, key, updated in zip(rows, (-250, -200, -195, -194),
                                         (-250, -200, -190, -194)):
                row["key"] = watch.iso(NOW + key)
                row["updated_at"] = watch.iso(NOW + updated)
            api, scan, events = LagApi(rows, []), None, []
            batch, scan, _ = watch.collect_batch(api, SUB, NOW - 3600, NOW, scan)
            events += batch
            self.assertEqual({e["object_id"] for e in batch}, {1, 2, 3})
            # #1 catches up and leaves the dense page for the live tail.
            rows[0]["key"] = watch.iso(NOW + 1)
            rows[0]["updated_at"] = watch.iso(NOW + 1)
            for _ in range(10):
                batch, scan, _ = watch.collect_batch(api, SUB, NOW, NOW, scan)
                events += batch
                if scan is None:
                    break
            self.assertIsNone(scan)
            self.assertEqual({e["object_id"] for e in events}, {1, 2, 3, 4})

    def test_index_lag_on_a_sparse_page_tail_cannot_skip_the_next_row(self):
        # #2955: same lag on a page sparse enough for the lag-margin seek. The
        # seek re-reads the lagged row and its successor out of updated order;
        # the already-read rows must not trip the order guard, and the next row
        # keyed NOW-640 is delivered.
        with mock.patch.object(watch, "PAGE_SIZE", 3):
            rows = [issue(n) for n in range(1, 5)]
            for row, key, updated in zip(rows, (-1000, -900, -700, -640),
                                         (-1000, -900, -600, -640)):
                row["key"] = watch.iso(NOW + key)
                row["updated_at"] = watch.iso(NOW + updated)
            api, scan, events = LagApi(rows, []), None, []
            for _ in range(10):
                batch, scan, _ = watch.collect_batch(api, SUB, NOW - 3600, NOW, scan)
                events += batch
                if scan is None:
                    break
            self.assertIsNone(scan)
            self.assertEqual({e["object_id"] for e in events}, {1, 2, 3, 4})

    def test_index_lag_after_a_write_past_the_window_is_read_next_window(self):
        # #2925: just after the projection wrote, GitHub still sorts and filters
        # the issue by its old key while the row carries the new updated_at.
        for size in (3, watch.PAGE_SIZE + 5):
            with self.subTest(full=size > watch.PAGE_SIZE):
                rows = [issue(n) for n in range(1, size + 1)]
                for n, row in enumerate(rows):
                    row["updated_at"] = row["key"] = watch.iso(NOW - 1000 + n)
                rows[0]["updated_at"] = watch.iso(NOW + 30)
                api, scan, events = LagApi(rows, [comment()]), None, []
                for _ in range(10):
                    batch, scan, _ = watch.collect_batch(api, SUB, NOW - 3600, NOW, scan)
                    events += batch
                    if scan is None:
                        break
                self.assertIsNone(scan)
                self.assertEqual({e["object_id"] for e in events if e["kind"] == "issue"},
                                 set(range(2, size + 1)))
                del rows[0]["key"]  # the index caught up
                events, _, _ = watch.collect_batch(api, SUB, NOW - watch.OVERLAP, NOW + 60, None)
                self.assertEqual([e["object_id"] for e in events if e["kind"] == "issue"], [1])

    def test_disorder_inside_the_window_or_on_a_full_page_tail_fails_closed(self):
        inside = [issue(n) for n in range(1, 4)]
        for n, row in enumerate(inside):
            row["updated_at"] = watch.iso(NOW - 1000 - n)
        tail = [issue(n) for n in range(1, watch.PAGE_SIZE + 1)]
        for n, row in enumerate(tail):
            row["updated_at"] = watch.iso(NOW + 30 if n == watch.PAGE_SIZE - 2 else
                                          NOW + 10 if n == watch.PAGE_SIZE - 1 else
                                          NOW - 1000 + n)
        for rows in (inside, tail):
            with self.subTest(full=len(rows) == watch.PAGE_SIZE):
                with self.assertRaisesRegex(plan_github.GithubError, "updated order is invalid"):
                    watch.collect_batch(Api(rows), SUB, NOW - 3600, NOW, None)

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

    def test_digest_links_existing_lots_and_requires_judgment(self):
        event = watch.collect(Api(comments=[comment()]), SUB, NOW)[0]
        text = watch.digest(SUB, [event], [dict(id=42, state="build", assignee="author",
                                                issue_ref="acme/app#1")])
        for part in ("#42 (build, author)", "acme/app#1", "ameesh decide ask",
                     "untrusted external data", "information → lot note and wake its author",
                     event["url"]):
            self.assertIn(part, text)
        self.assertNotIn("wake its assignee", text)
        instruction = watch.instruction(SUB, event, [])
        self.assertIn("information → lot note and wake its author", instruction)
        self.assertNotIn("wake its assignee", instruction)

    def test_digest_is_bounded_and_every_entry_keeps_its_link(self):
        events = watch.collect(Api([issue(n) for n in range(1, 4)]), SUB, NOW)
        text = watch.digest(SUB, events)
        self.assertIn("3 input(s)", text)
        for event in events:
            self.assertIn(event["url"], text)


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

    def test_no_implicit_role_or_foreign_project_is_ignored_and_logged(self):
        for change in (dict(roles=[]), dict(effective_projects=["other"])):
            warnings = []
            with self.subTest(change=change):
                self.assertEqual(watch.subscriptions([self.bundle(**change)],
                                                     log=warnings.append), [])
                self.assertTrue(warnings)

    def test_invalid_cadence_and_conflicting_designation_are_isolated(self):
        for interval in (True, 0, 3601, float("nan"), "900"):
            warnings = []
            fiche = NS(data={"github_watch": {
                "app": {"repos": ["acme/app"], "interval": interval}}})
            with self.subTest(interval=interval):
                self.assertEqual(watch.subscriptions([self.bundle(fiche=fiche)],
                                                     log=warnings.append), [])
                self.assertIn("interval", warnings[0])
        warnings = []
        kept = watch.subscriptions([self.bundle(), self.bundle(title="other")],
                                   log=warnings.append)
        self.assertEqual(kept, [SUB])
        self.assertIn("multiple", warnings[0])

    def test_one_bad_declaration_does_not_disable_the_valid_ones(self):
        warnings = []
        kept = watch.subscriptions([self.bundle(title="bad", roles=[]), self.bundle()],
                                   log=warnings.append)
        self.assertEqual(kept, [SUB])
        self.assertIn("role github-watch", warnings[0])

    def test_bad_project_and_repository_keep_the_siblings(self):
        fiche = NS(data={"github_watch": {
            "app": {"repos": ["acme/app", "not-a-repo", "acme/site"]},
            "other": {"repos": []}}})
        warnings = []
        kept = watch.subscriptions([self.bundle(
            effective_projects=["app", "other"], fiche=fiche)], log=warnings.append)
        self.assertEqual([(sub.project, sub.repo) for sub in kept],
                         [("app", "acme/app"), ("app", "acme/site")])
        self.assertTrue(any("not-a-repo" in warning for warning in warnings))
        self.assertTrue(any("other" in warning for warning in warnings))


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
        messages = self.messages()
        self.assertEqual(len(messages), 1)
        self.assertIn("2 input(s)", messages[0]["body"])
        self.assertIn("acme/app#1", messages[0]["body"])
        self.assertEqual(messages[0]["payload"]["github_watch"]["count"], 2)
        self.watcher(api).tick(self.db, NOW + 901)
        self.assertEqual(len(self.messages()), 1)
        api.comments[0]["body"] = "Updated information"
        self.watcher(api).tick(self.db, NOW + 1802)
        self.assertEqual(len(self.messages()), 2)
        self.assertEqual(self.messages()[-1]["payload"]["github_watch"]["items"][0]["issue_ref"],
                         "acme/app#1")
        self.assertEqual(storage.of(self.db).github_watch.cursor("app", "acme/app"), NOW + 1802)

    def test_committed_mail_is_written_and_indexed_in_the_thread(self):
        self.watcher(Api([issue()], [comment()])).tick(self.db, NOW)
        messages = self.messages()
        self.assertEqual(len(messages), 1)
        transport = fil.transport_for(self.db.cfg)
        entries = [entry for thread in transport.threads()
                   for entry in transport.read(thread)
                   if "GitHub watch" in entry.text]
        self.assertEqual(len(entries), 1)
        self.assertEqual({mid for entry in entries for mid in entry.meta["ids"]},
                         {int(message["id"]) for message in messages})
        indexed = storage.of(self.db).threads.indexed()
        self.assertTrue(indexed)

    def test_delivery_failure_rolls_back_cursor_events_and_all_messages(self):
        api = Api([issue()], [comment()])
        calls = []

        def fail(*a, **kw):
            calls.append(1)
            raise RuntimeError("mailbox failure")

        with mock.patch.object(mail, "send", side_effect=fail):
            self.assertEqual(self.watcher(api).tick(self.db, NOW), [])
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.messages(), [])
        self.assertIsNone(storage.of(self.db).github_watch.cursor("app", "acme/app"))
        self.assertEqual(self.db.query("SELECT * FROM github_watch_events"), [])
        self.watcher(api).tick(self.db, NOW + 901)
        self.assertEqual(len(self.messages()), 1)

    def test_a_burst_is_one_bounded_digest_and_cursor_waits_for_the_rest(self):
        api = Api([issue(n) for n in range(1, 6)])
        watcher = self.watcher(api)
        with mock.patch.object(watch, "DIGEST_MAX_ITEMS", 3):
            watcher.tick(self.db, NOW)
            messages = self.messages()
            self.assertEqual(len(messages), 1)
            self.assertIn("3 input(s)", messages[0]["body"])
            for n in (1, 2, 3):
                self.assertIn("acme/app#%d" % n, messages[0]["body"])
            self.assertNotIn("acme/app#4", messages[0]["body"])
            self.assertEqual(len(self.db.query("SELECT * FROM github_watch_events")), 3)
            # The bootstrap cursor row exists, but it has not advanced: the
            # remaining inputs will be re-read and delivered by the next pass.
            self.assertEqual(storage.of(self.db).github_watch.cursor("app", "acme/app"),
                             NOW - watch.BOOTSTRAP)
            # A leftover digest continues on the next notifier tick, without
            # waiting for the repository cadence.
            watcher.tick(self.db, NOW)
            messages = self.messages()
            self.assertEqual(len(messages), 2)
            self.assertIn("2 input(s)", messages[-1]["body"])
            self.assertEqual(storage.of(self.db).github_watch.cursor("app", "acme/app"), NOW)
            watcher.tick(self.db, NOW + 901)
            self.assertEqual(len(self.messages()), 2)
            self.assertEqual(storage.of(self.db).github_watch.cursor("app", "acme/app"), NOW + 901)

    def test_leftover_digest_holds_the_scan_checkpoint(self):
        api = Api([issue(n) for n in range(1, 26)])
        with mock.patch.object(watch, "PAGE_SIZE", 10), \
                mock.patch.object(watch, "DIGEST_MAX_ITEMS", 3):
            events, progress, until = watch.collect_batch(
                api, SUB, NOW - 3600, NOW, None)
            self.assertEqual(len(events), 10)
            self.assertIsNotNone(progress)
            for expected in (3, 3, 3):
                self.assertEqual(watch.deliver(
                    self.db, SUB, events, until, [], self.cfg.host, partial=True,
                    scan_before=None, scan_after=progress), expected)
                self.assertIsNone(storage.of(self.db).github_watch.scan("app", "acme/app"))
                self.assertEqual(storage.of(self.db).github_watch.cursor("app", "acme/app"),
                                 NOW - watch.BOOTSTRAP)
            self.assertEqual(watch.deliver(
                self.db, SUB, events, until, [], self.cfg.host, partial=True,
                scan_before=None, scan_after=progress), 1)
        self.assertEqual(storage.of(self.db).github_watch.scan("app", "acme/app"), progress)
        self.assertEqual(storage.of(self.db).github_watch.cursor("app", "acme/app"),
                         NOW - watch.BOOTSTRAP)
        messages = self.messages()
        self.assertEqual(len(messages), 4)
        text = "\n".join(message["body"] for message in messages)
        for n in range(1, 11):
            self.assertEqual(text.count("- acme/app#%d ·" % n), 1)

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
            messages = self.messages()
            self.assertEqual(len(messages), 1)
            self.assertIn("2 input(s)", messages[0]["body"])
            for n in (10, 11):
                self.assertIn("#issuecomment-%d" % n, messages[0]["body"])
            progress = storage.of(self.db).github_watch.scan("app", "acme/app")
            self.assertEqual((progress["kind"], progress["page"]), ("comment", 2))
            self.assertLess(storage.of(self.db).github_watch.cursor("app", "acme/app"), NOW)
            self.watcher(api).tick(self.db, NOW + 901)
            self.watcher(api).tick(self.db, NOW + 902)
        messages = self.messages()
        self.assertEqual(len(messages), 3)
        self.assertIn("2 input(s)", messages[1]["body"])
        self.assertIn("1 input(s)", messages[2]["body"])
        for n in (12, 13):
            self.assertIn("#issuecomment-%d" % n, messages[1]["body"])
        self.assertIn("#issuecomment-14", messages[2]["body"])
        for n in (10, 11):
            self.assertNotIn("#issuecomment-%d" % n, messages[1]["body"])
            self.assertNotIn("#issuecomment-%d" % n, messages[2]["body"])
        self.assertIsNone(storage.of(self.db).github_watch.scan("app", "acme/app"))
        self.assertEqual(storage.of(self.db).github_watch.cursor("app", "acme/app"), NOW)
        self.assertIn("page=2", api.calls[2])

    def test_drift_checkpoint_replays_without_duplicate_mail_or_early_advance(self):
        api = Api([issue(n) for n in range(1, 7)])
        watcher = self.watcher(api)
        with mock.patch.object(watch, "PAGE_SIZE", 2):
            watcher.tick(self.db, NOW)
            watcher.tick(self.db, NOW + 1)
            self.assertEqual(len(self.messages()), 2)
            api.issues.append(api.issues.pop(0))
            watcher.tick(self.db, NOW + 2)
            # #2955: the moved prefix exposes #5 and #6; they are delivered in
            # this pass with the page read before the check, not replayed.
            self.assertEqual(len(self.messages()), 3)
            self.assertEqual(storage.of(self.db).github_watch.scan("app", "acme/app")["page"], 4)
            self.assertLess(storage.of(self.db).github_watch.cursor("app", "acme/app"), NOW)
            for delta in range(3, 8):
                watcher.tick(self.db, NOW + delta)
        self.assertEqual(len(self.messages()), 3)
        self.assertIsNone(storage.of(self.db).github_watch.scan("app", "acme/app"))
        self.assertEqual(storage.of(self.db).github_watch.cursor("app", "acme/app"), NOW)

    def test_partial_scan_continues_on_next_tick_even_with_hourly_interval(self):
        api = Api(comments=[comment(n) for n in range(10, 15)])
        watcher = watch.Watcher(self.cfg, api=api,
                                declarations=[dataclasses.replace(SUB, interval=3600)])
        with mock.patch.object(watch, "PAGE_SIZE", 2):
            for delta, expected in ((0, 1), (1, 2), (2, 3)):
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
            self.assertEqual(len(self.messages()), 1)
            self.watcher(api).tick(self.db, NOW + 1802)
            self.watcher(api).tick(self.db, NOW + 1803)
        messages = self.messages()
        self.assertEqual(len(messages), 3)
        self.assertIn("2 input(s)", messages[1]["body"])
        self.assertIn("1 input(s)", messages[2]["body"])
        self.assertIn("#issuecomment-14", messages[2]["body"])

    def test_stale_partial_batch_cannot_rewind_the_checkpoint(self):
        api = Api(comments=[comment(n) for n in range(10, 15)])
        with mock.patch.object(watch, "PAGE_SIZE", 2):
            events, progress, until = watch.collect_batch(api, SUB, NOW - 3600, NOW, None)
            self.assertEqual(watch.deliver(self.db, SUB, events, until, [], self.cfg.host,
                             partial=True, scan_after=progress), 2)
            self.assertEqual(watch.deliver(self.db, SUB, events, until, [], self.cfg.host,
                             partial=True, scan_after=progress), 0)
        self.assertEqual(storage.of(self.db).github_watch.scan("app", "acme/app"), progress)
        messages = self.messages()
        self.assertEqual(len(messages), 1)
        self.assertIn("2 input(s)", messages[0]["body"])

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

    def test_failed_pass_is_retried_before_an_hourly_cadence(self):
        api = Api([issue()])
        watcher = watch.Watcher(self.cfg, api=api,
                                declarations=[dataclasses.replace(SUB, interval=3600)])
        with mock.patch.object(api, "get", side_effect=RuntimeError("GitHub unavailable")):
            self.assertEqual(watcher.tick(self.db, NOW), [])
        api.calls.clear()
        watcher.tick(self.db, NOW + watch.RETRY - 1)
        self.assertEqual(api.calls, [])
        watcher.tick(self.db, NOW + watch.RETRY)
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
