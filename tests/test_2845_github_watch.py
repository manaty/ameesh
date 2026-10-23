# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import dataclasses
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
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
        # #2955: page 1, then (page, predecessor) for pages 2 and 3, then the
        # short page 3 and the comment page share the last pass.
        self.assertLessEqual(len(api.calls), 6)
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
        self.assertEqual(until, NOW - watch.INDEX_LAG)
        for _ in range(110):
            api.calls.clear()
            more, scan, _ = watch.collect_batch(api, SUB, NOW, NOW + 500, scan)
            events += more
            self.assertLessEqual(len(api.calls), watch.PAGES_PER_TICK)
            if scan is None:
                break
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
                self.assertEqual(until, NOW - watch.INDEX_LAG)
                self.assertTrue(set(range(1, 601)) <= {e["object_id"] for e in events})
                self.assertLess(tick, 10)

    def test_timestamp_ties_and_comment_window_share_the_settled_bound(self):
        # #2955: the bounded window keeps equal timestamps together; the
        # comment stream keeps the original window start.
        rows = [issue(n) for n in range(1, 401)]
        for n, row in enumerate(rows):
            row["updated_at"] = watch.iso(NOW - (1000 if n < 50 else 800 if n < 350 else 500))
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

    def finish(self, api, since, now, scan=None):
        """Drive one bounded window to its end (the watcher does this by ticks)."""
        events = []
        for _ in range(50):
            batch, scan, until = watch.collect_batch(api, SUB, since, now, scan)
            events += batch
            if scan is None:
                return events, until
        self.fail("scan did not finish")

    def test_configured_index_lag_controls_the_settled_bound(self):
        row = dict(issue(), updated_at=watch.iso(NOW - 1))
        events, _, _ = watch.collect_batch(Api([row]), SUB, NOW - 3600, NOW, None)
        self.assertEqual(events, [])  # default 300 s: NOW-1 may still move
        immediate = dataclasses.replace(SUB, index_lag=0)
        events, _, _ = watch.collect_batch(Api([row]), immediate, NOW - 3600, NOW, None)
        self.assertEqual([e["object_id"] for e in events], [1])

    def test_window_defers_live_entries_to_next_scan(self):
        api = UpdatedApi(issues=[dict(issue(), updated_at=watch.iso(NOW + 1))])
        events, done, until = watch.collect_batch(api, SUB, NOW - 3600, NOW, None)
        self.assertEqual(events, [])
        self.assertIsNone(done)
        self.assertEqual(until, NOW - watch.INDEX_LAG)
        # The next window starts OVERLAP before the committed settled bound,
        # so the row stamped inside the latency window is read then.
        events, done, _ = watch.collect_batch(
            api, SUB, NOW - watch.OVERLAP, NOW + watch.INDEX_LAG + 1, None)
        self.assertEqual([e["object_id"] for e in events], [1])
        self.assertIsNone(done)

    def test_b1_dense_page_tail_lag_is_deferred_not_skipped(self):
        # B1 #2955: the last row of a full page carries a newer updated_at than
        # its index key. The old seek used the stamp and skipped the successor.
        # A row stamped after U is now deferred; the next window reads it once
        # the index has caught up (250 s <= INDEX_LAG).
        with mock.patch.object(watch, "PAGE_SIZE", 3):
            rows = [issue(n) for n in range(1, 5)]
            for row, key, updated in zip(rows, (-1000, -900, -800, -690),
                                         (-1000, -900, -250, -690)):
                row["key"] = watch.iso(NOW + key)
                row["updated_at"] = watch.iso(NOW + updated)
            api = LagApi(rows, [])
            first, until = self.finish(api, NOW - 3600, NOW)
            self.assertEqual({e["object_id"] for e in first}, {1, 2, 4})
            rows[2]["key"] = watch.iso(NOW - 250)  # index caught up
            more, _ = self.finish(api, until - watch.OVERLAP,
                                  NOW + watch.INDEX_LAG + 1)
            self.assertEqual({e["object_id"] for e in first + more}, {1, 2, 3, 4})

    def test_b2_multi_page_prefix_shift_restarts_before_skipping_a_row(self):
        # B2 #2955: moving more than PAGE_SIZE rows of the prefix forward can
        # push an undelivered row back before the page being checked. The
        # bounded window anchors the re-read; no row of the window is skipped.
        with mock.patch.object(watch, "PAGE_SIZE", 3):
            rows = [issue(n) for n in range(1, 13)]
            for n, row in enumerate(rows):
                row["updated_at"] = watch.iso(NOW - 1000 + n)
            api = UpdatedApi(rows, [])
            events, scan, _ = watch.collect_batch(api, SUB, NOW - 3600, NOW, None)
            more, scan, _ = watch.collect_batch(api, SUB, NOW, NOW, scan)
            events += more
            self.assertEqual({e["object_id"] for e in events}, set(range(1, 7)))
            for row in rows[:4]:
                row["updated_at"] = watch.iso(NOW + 1)
            more, _ = self.finish(api, NOW, NOW, scan)
            events += more
            self.assertEqual({e["object_id"] for e in events}, set(range(1, 13)))

    def test_b3_moved_boundary_row_cannot_mask_a_multi_page_shift(self):
        # B3 #2955: the boundary row itself advanced, so an id overlap no
        # longer proved the shift fit in one page. It is now outside the
        # settled window and deferred; #7 and #8 are still delivered.
        with mock.patch.object(watch, "PAGE_SIZE", 3):
            rows = [issue(n) for n in range(1, 13)]
            for n, row in enumerate(rows):
                row["updated_at"] = watch.iso(NOW - 1000 + n)
            api = UpdatedApi(rows, [])
            events, scan, _ = watch.collect_batch(api, SUB, NOW - 3600, NOW, None)
            more, scan, _ = watch.collect_batch(api, SUB, NOW, NOW, scan)
            events += more
            self.assertEqual({e["object_id"] for e in events}, set(range(1, 7)))
            for row in rows[:4]:
                row["updated_at"] = watch.iso(NOW + 1)
            rows[5]["updated_at"] = watch.iso(NOW - 242)
            more, _ = self.finish(api, NOW, NOW, scan)
            events += more
            self.assertEqual({e["object_id"] for e in events}, set(range(1, 13)))

    def test_b3_index_catch_up_on_the_boundary_row_is_deferred(self):
        # B3 #2955 (LagApi): the boundary row advances while its key still lags
        # at the old place. Under the bounded window its new stamp is after U,
        # so it is deferred instead of masking the shift of #7/#8.
        with mock.patch.object(watch, "PAGE_SIZE", 3):
            rows = [issue(n) for n in range(1, 13)]
            for n, row in enumerate(rows):
                row["key"] = row["updated_at"] = watch.iso(NOW - 1000 + n)
            api = LagApi(rows, [])
            events, scan, until = watch.collect_batch(api, SUB, NOW - 3600, NOW, None)
            more, scan, _ = watch.collect_batch(api, SUB, NOW, NOW, scan)
            events += more
            self.assertEqual({e["object_id"] for e in events}, set(range(1, 7)))
            for row in rows[:4]:
                row["key"] = row["updated_at"] = watch.iso(NOW + 1)
            rows[5]["key"] = watch.iso(NOW - 995)
            rows[5]["updated_at"] = watch.iso(NOW + 1)
            more, _ = self.finish(api, NOW, NOW, scan)
            events += more
            self.assertEqual({e["object_id"] for e in events}, set(range(1, 13)))

    def test_b4_boundary_compensation_defers_the_shifted_tail(self):
        # B4 #2955: after two calls deliver #1..#6, move #1..#3 to the live tail
        # and #4..#6 to NOW-259/-258/-257. The previous page keeps the same ids
        # with new stamps, so the old shift formula accepted d=0 and delivered
        # #10..#12 while #7..#9 moved before the cursor. With U = scan start -
        # INDEX_LAG, every moved row is deferred; the next window reads it.
        with mock.patch.object(watch, "PAGE_SIZE", 3):
            start = NOW + 30  # U = start - INDEX_LAG = NOW - 270
            rows = [issue(n) for n in range(1, 13)]
            for n, row in enumerate(rows):
                row["updated_at"] = watch.iso(NOW - 300 + 5 * n)
            api = UpdatedApi(rows, [])
            events, scan, until = watch.collect_batch(
                api, SUB, start - 3600, start, None)
            more, scan, _ = watch.collect_batch(api, SUB, start, start, scan)
            events += more
            self.assertEqual({e["object_id"] for e in events}, set(range(1, 7)))
            self.assertEqual(scan["page"], 3)
            for row in rows[:3]:
                row["updated_at"] = watch.iso(NOW + 1)
            for row, stamp in zip(rows[3:6], (NOW - 259, NOW - 258, NOW - 257)):
                row["updated_at"] = watch.iso(stamp)
            # The compensation is inside the latency window: it must not be
            # accepted as a stable boundary.
            more, _ = self.finish(api, start, start, scan)
            events += more
            self.assertEqual({e["object_id"] for e in events}, set(range(1, 8)))
            more, _ = self.finish(api, until - watch.OVERLAP,
                                  NOW + 60, None)
            events += more
            self.assertEqual({e["object_id"] for e in events}, set(range(1, 13)))

    def test_b4_lag_api_variant_does_not_lose_the_shifted_tail(self):
        # B4 #2955, LagApi variant: the writes of #4..#6 happen before the
        # reads, so the previous page carries exactly the same stamps on both
        # reads and an id/stamp shift check sees nothing. The bounded window
        # still defers those rows (> U) and reads #8..#12 in the next window;
        # #7 is stamped exactly at U and stays inside.
        with mock.patch.object(watch, "PAGE_SIZE", 3):
            start = NOW + 30  # U = NOW - 270
            rows = [issue(n) for n in range(1, 13)]
            for n, row in enumerate(rows):
                row["key"] = row["updated_at"] = watch.iso(NOW - 300 + 5 * n)
            api = LagApi(rows, [])
            for row in rows[:3]:
                row["updated_at"] = watch.iso(NOW + 1)
            for row, stamp in zip(rows[3:6], (NOW - 259, NOW - 258, NOW - 257)):
                row["updated_at"] = watch.iso(stamp)
            events, scan, until = watch.collect_batch(
                api, SUB, start - 3600, start, None)
            more, _ = self.finish(api, start, start, scan)
            events += more
            self.assertEqual({e["object_id"] for e in events}, {7})
            # The index catches up for the deferred rows before the next window.
            for row in rows:
                if row["updated_at"] <= watch.iso(NOW + 60 - watch.INDEX_LAG):
                    row["key"] = row["updated_at"]
            more, until = self.finish(api, until - watch.OVERLAP, NOW + 60, None)
            events += more
            self.assertEqual({e["object_id"] for e in events}, set(range(4, 13)))
            # #1..#3 were moved to NOW+1 before the scan: they are deferred
            # too, then read once their own stamp is inside the bound.
            for row in rows[:3]:
                row["key"] = row["updated_at"]
            more, _ = self.finish(api, until - watch.OVERLAP, NOW + 301, None)
            events += more
            self.assertEqual({e["object_id"] for e in events}, set(range(1, 13)))

    def test_b5_recent_page_must_not_end_the_stream(self):
        # B5 #2955: while the first page is read, #1..#3 get updated_at > until
        # but keep their old keys. That page has no settled row, yet #4 on the
        # next page is settled. The scan must not stop on the first recent
        # page; it reads #4 now and #1..#3 in the next window.
        with mock.patch.object(watch, "PAGE_SIZE", 3):
            rows = [issue(n) for n in range(1, 5)]
            for n, row in enumerate(rows):
                row["key"] = row["updated_at"] = watch.iso(NOW - 1000 + n)
            api = LagApi(rows, [])
            get = api.get
            mutated = []

            def mutate(path, **kw):
                out = get(path, **kw)
                if not mutated:
                    mutated.append(True)
                    for row in rows[:3]:
                        row["updated_at"] = watch.iso(NOW + 1)
                return out

            with mock.patch.object(api, "get", side_effect=mutate):
                events, scan, until = watch.collect_batch(
                    api, SUB, NOW - 3600, NOW, None)
                more, _ = self.finish(api, NOW, NOW, scan)
            events += more
            self.assertEqual({e["object_id"] for e in events}, {4})
            self.assertEqual(until, NOW - watch.INDEX_LAG)
            for row in rows[:3]:
                row["key"] = row["updated_at"]
            more, _ = self.finish(api, until - watch.OVERLAP, NOW + 400, None)
            events += more
            self.assertEqual({e["object_id"] for e in events}, {1, 2, 3, 4})

    def test_surely_indexed_row_past_the_window_stops_the_stream(self):
        # Contract #2955: once a row is older than INDEX_LAG at the read, its
        # key is its stamp. A surely indexed row past U proves every following
        # key is past U too: the stream can stop, after delivering the settled
        # rows of the page.
        with mock.patch.object(watch, "PAGE_SIZE", 2):
            rows = [issue(n) for n in range(1, 5)]
            rows[2]["updated_at"] = watch.iso(NOW - 250)  # > U at scan start
            api = Api(rows, [])
            events, scan, _ = watch.collect_batch(
                api, SUB, NOW - 3600, NOW, None)
            more, scan, _ = watch.collect_batch(
                api, SUB, NOW, NOW + 100, scan)
            events += more
        self.assertEqual({e["object_id"] for e in events}, {1, 2, 4})
        self.assertIsNone(scan)
        self.assertFalse(any("page=3" in call for call in api.calls))

    def test_b5_recent_burst_does_not_hide_a_settled_row_behind_it(self):
        # mesh-design #2955: 13 recent rows with old keys (5 pages) followed by
        # a settled row. A cutoff after a few full pages would commit U and
        # lose it. The scan skips recent pages without committing and reaches
        # the settled row in the same pass.
        with mock.patch.object(watch, "PAGE_SIZE", 3):
            rows = [issue(n) for n in range(1, 15)]
            for n, row in enumerate(rows[:13]):
                row["key"] = watch.iso(NOW - 2000 + n)
                row["updated_at"] = watch.iso(NOW + 1)
            rows[13]["key"] = rows[13]["updated_at"] = watch.iso(NOW - 1000)
            api = LagApi(rows, [])
            events, until = self.finish(api, NOW - 3600, NOW)
            self.assertEqual({e["object_id"] for e in events}, {14})
            self.assertEqual(until, NOW - watch.INDEX_LAG)
            for row in rows[:13]:
                row["key"] = row["updated_at"]
            more, _ = self.finish(api, until - watch.OVERLAP, NOW + 400, None)
            events += more
            self.assertEqual({e["object_id"] for e in events}, set(range(1, 15)))

    def test_b7_recent_burst_bounded_by_pages_does_not_conclude(self):
        # B7 #2955: 13 rows updated during the first GET, keys still old, and
        # #14 settled behind them. The pass must read through the recent pages
        # (within the budget) and deliver #14; no cursor U is committed on a
        # page count.
        with mock.patch.object(watch, "PAGE_SIZE", 3):
            rows = [issue(n) for n in range(1, 15)]
            for n, row in enumerate(rows):
                row["key"] = row["updated_at"] = watch.iso(NOW - 1000 + n)
            api = LagApi(rows, [])
            get = api.get
            mutated = []

            def mutate(path, **kw):
                out = get(path, **kw)
                if not mutated:
                    mutated.append(True)
                    for row in rows[:13]:
                        row["updated_at"] = watch.iso(NOW + 1)
                return out

            with mock.patch.object(api, "get", side_effect=mutate):
                events, scan, until = watch.collect_batch(
                    api, SUB, NOW - 3600, NOW, None)
                more, _ = self.finish(api, NOW, NOW, scan)
            events += more
            self.assertEqual({e["object_id"] for e in events}, {14})
            for row in rows[:13]:
                row["key"] = row["updated_at"]
            more, _ = self.finish(api, until - watch.OVERLAP, NOW + 400, None)
            events += more
            self.assertEqual({e["object_id"] for e in events}, set(range(1, 15)))

    def test_b8a_safe_stop_still_delivers_the_anchor_successor(self):
        # B8a #2955: #1 moves to the tail; the page is surely indexed. The
        # previous page is [2,3,4] and the anchor [1,2,3] proves #4 follows
        # the accepted prefix. #4 must be delivered before the stream stops.
        with mock.patch.object(watch, "PAGE_SIZE", 3):
            rows = [issue(n) for n in range(1, 7)]
            for n, row in enumerate(rows):
                row["key"] = row["updated_at"] = watch.iso(NOW - 1000 + n)
            api = LagApi(rows, [])
            events, scan, until = watch.collect_batch(
                api, SUB, NOW - 3600, NOW, None)
            self.assertEqual({e["object_id"] for e in events}, {1, 2, 3})
            rows[0]["key"] = rows[0]["updated_at"] = watch.iso(NOW + 1)
            more, scan, _ = watch.collect_batch(
                api, SUB, NOW, NOW + 302, scan)
            events += more
            self.assertIsNone(scan)
            self.assertEqual({e["object_id"] for e in events}, {1, 2, 3, 4, 5, 6})

    def test_b8b_safe_stop_checks_the_anchor_before_concluding(self):
        # B8b #2955: #1..#6 move to the tail; page 3 is surely indexed and the
        # previous page is [10,11,12]. The anchor [4,5,6] is not there; the
        # scan must read back and restart, delivering #7..#12 instead of
        # concluding on the recent page.
        with mock.patch.object(watch, "PAGE_SIZE", 3):
            rows = [issue(n) for n in range(1, 13)]
            for n, row in enumerate(rows):
                row["key"] = row["updated_at"] = watch.iso(NOW - 1000 + n)
            api = LagApi(rows, [])
            events, scan, _ = watch.collect_batch(
                api, SUB, NOW - 3600, NOW, None)
            more, scan, _ = watch.collect_batch(api, SUB, NOW, NOW, scan)
            events += more
            self.assertEqual({e["object_id"] for e in events}, set(range(1, 7)))
            for row in rows[:6]:
                row["key"] = row["updated_at"] = watch.iso(NOW + 1)
            more, _ = self.finish(api, NOW, NOW + 302, scan)
            events += more
            self.assertEqual({e["object_id"] for e in events}, set(range(1, 13)))

    def test_b9_offset_suspension_without_anchor_cannot_hide_a_row(self):
        # B9 #2955: 31 rows become recent during the first GET, keys old; #32
        # is stable behind them. A checkpointed page offset could be invalidated
        # when the keys catch up and #32 recedes to page 1. Without an accepted
        # anchor the pass reads through to the end and delivers #32.
        with mock.patch.object(watch, "PAGE_SIZE", 3):
            rows = [issue(n) for n in range(1, 33)]
            for n, row in enumerate(rows):
                row["key"] = row["updated_at"] = watch.iso(NOW - 1000 + n)
            api = LagApi(rows, [])
            get = api.get
            mutated = []

            def mutate(path, **kw):
                out = get(path, **kw)
                if not mutated:
                    mutated.append(True)
                    for row in rows[:31]:
                        row["updated_at"] = watch.iso(NOW + 1)
                return out

            with mock.patch.object(api, "get", side_effect=mutate):
                events, scan, until = watch.collect_batch(
                    api, SUB, NOW - 3600, NOW, None)
                more, _ = self.finish(api, NOW, NOW, scan)
            events += more
            self.assertEqual({e["object_id"] for e in events}, {32})
            for row in rows[:31]:
                row["key"] = row["updated_at"]
            more, _ = self.finish(api, until - watch.OVERLAP, NOW + 700, None)
            events += more
            self.assertEqual({e["object_id"] for e in events}, set(range(1, 33)))

    def test_b10_settled_row_pushed_into_a_skipped_page_is_recovered(self):
        # B10 #2955 (cx1): page 1 is full of recent rows whose index keys still
        # lag, so it has no settled row and is skipped. Before page 2 is read
        # their keys catch up: #4 (settled) recedes from page 2 to page 1 and
        # page 2 becomes [3]. Concluding on the short page without re-reading
        # the skipped page loses #4, whose updated_at is older than
        # U - OVERLAP and would never be read again.
        with mock.patch.object(watch, "PAGE_SIZE", 3):
            rows = [issue(n) for n in range(1, 5)]
            for n, row in enumerate(rows):
                row["key"] = watch.iso(NOW - 1000 + n)
            for row in rows[:3]:
                row["updated_at"] = watch.iso(NOW - 1)
            rows[3]["key"] = rows[3]["updated_at"] = watch.iso(NOW - 997)
            api = LagApi(rows, [])
            get = api.get
            mutated = []

            def mutate(path, **kw):
                if "page=2" in path and not mutated:
                    mutated.append(True)
                    for row in rows[:3]:
                        row["key"] = row["updated_at"]
                return get(path, **kw)

            with mock.patch.object(api, "get", side_effect=mutate):
                events, until = self.finish(api, NOW - 3600, NOW)
            self.assertEqual({e["object_id"] for e in events}, {4})
            self.assertEqual(until, NOW - watch.INDEX_LAG)
            for row in rows[:3]:
                row["key"] = row["updated_at"]
            more, _ = self.finish(api, until - watch.OVERLAP, NOW + 400, None)
            events += more
            self.assertEqual({e["object_id"] for e in events}, set(range(1, 5)))

    def test_b10_comment_stream_recovers_a_settled_comment_pushed_into_a_skipped_page(self):
        # B10 #2955, comment stream: same receding row as the issue case.
        with mock.patch.object(watch, "PAGE_SIZE", 3):
            rows = [comment(n) for n in range(10, 14)]
            for n, row in enumerate(rows):
                row["key"] = watch.iso(NOW - 1000 + n)
            for row in rows[:3]:
                row["updated_at"] = watch.iso(NOW - 1)
            rows[3]["key"] = rows[3]["updated_at"] = watch.iso(NOW - 997)
            api = LagApi(comments=rows)
            get = api.get
            mutated = []

            def mutate(path, **kw):
                if "page=2" in path and not mutated:
                    mutated.append(True)
                    for row in rows[:3]:
                        row["key"] = row["updated_at"]
                return get(path, **kw)

            with mock.patch.object(api, "get", side_effect=mutate):
                events, until = self.finish(api, NOW - 3600, NOW)
            self.assertEqual({e["object_id"] for e in events}, {13})
            for row in rows[:3]:
                row["key"] = row["updated_at"]
            more, _ = self.finish(api, until - watch.OVERLAP, NOW + 400, None)
            events += more
            self.assertEqual({e["object_id"] for e in events}, set(range(10, 14)))

    def test_b10_real_page_size_recovers_the_row_pushed_into_the_skipped_page(self):
        # B10 #2955 at the real page size: 100 recent rows fill page 1; #101
        # recedes into it when their keys catch up before page 2 is read.
        with mock.patch.object(watch, "PAGE_SIZE", 100):
            rows = [issue(n) for n in range(1, 102)]
            for n, row in enumerate(rows):
                row["key"] = watch.iso(NOW - 2000 + n)
            for row in rows[:100]:
                row["updated_at"] = watch.iso(NOW - 1)
            rows[100]["key"] = rows[100]["updated_at"] = watch.iso(NOW - 1500)
            api = LagApi(rows, [])
            get = api.get
            mutated = []

            def mutate(path, **kw):
                if "page=2" in path and not mutated:
                    mutated.append(True)
                    for row in rows[:100]:
                        row["key"] = row["updated_at"]
                return get(path, **kw)

            with mock.patch.object(api, "get", side_effect=mutate):
                events, until = self.finish(api, NOW - 3600, NOW)
            self.assertEqual({e["object_id"] for e in events}, {101})
            self.assertEqual(until, NOW - watch.INDEX_LAG)

    def test_b10_skipped_pages_are_rechecked_last_to_first(self):
        # B10 #2955: #101 recedes from page 3 to page 2 when the keys of page 2
        # catch up, then from page 2 to page 1 while the skipped pages are
        # re-read. Ascending order would read page 1 before the second move
        # and miss #101; last-to-first catches it on page 1 after reading
        # page 2.
        with mock.patch.object(watch, "PAGE_SIZE", 3):
            rows = [issue(n) for n in range(1, 7)]
            for n, row in enumerate(rows):
                row["key"] = watch.iso(NOW - 2500 + n)
                row["updated_at"] = watch.iso(NOW - 1)
            stable = issue(101)
            stable["key"] = stable["updated_at"] = watch.iso(NOW - 1000)
            api = LagApi(rows + [stable], [])
            get = api.get
            moved = []

            def mutate(path, **kw):
                page = int(parse_qs(urlsplit(path).query)["page"][0])
                if page == 3 and not moved:
                    moved.append("to-page-2")
                    for row in rows[3:]:
                        row["key"] = row["updated_at"]
                elif page == 2 and moved:
                    moved.append("to-page-1")
                    for row in rows[:3]:
                        row["key"] = row["updated_at"]
                return get(path, **kw)

            with mock.patch.object(api, "get", side_effect=mutate):
                events, until = self.finish(api, NOW - 3600, NOW)
        self.assertEqual({e["object_id"] for e in events}, {101})
        self.assertEqual(until, NOW - watch.INDEX_LAG)

    def test_skipped_page_recheck_keeps_the_input_checkpoint_unchanged(self):
        # deliver compares scan_before with the durable checkpoint. A resumed
        # recheck must not mutate that expected value through shared children.
        rows = [issue(n) for n in range(1, 11)]
        for n, row in enumerate(rows):
            row["key"] = watch.iso(NOW - 2000 + n)
            row["updated_at"] = watch.iso(NOW - 1)
        rows[-1]["key"] = rows[-1]["updated_at"] = watch.iso(NOW - 1000)
        api, scan, events = LagApi(rows), None, []
        with mock.patch.object(watch, "PAGE_SIZE", 3), \
                mock.patch.object(watch, "PAGES_PER_TICK", 2):
            for _ in range(8):
                before = deepcopy(scan)
                batch, after, until = watch.collect_batch(
                    api, SUB, NOW - 3600, NOW, scan)
                self.assertEqual(scan, before)
                events.extend(batch)
                scan = after
                if scan is None:
                    break
        self.assertIsNone(scan)
        self.assertEqual({e["object_id"] for e in events}, {10})
        self.assertEqual(until, NOW - watch.INDEX_LAG)

    def test_b11_settled_row_pushed_into_a_mixed_page_is_recovered(self):
        # B11 #2955 (cx2): page 1 mixes one settled row with recent ones and
        # is accepted; page 2 is full of recent rows and is traversed with an
        # anchor, so the old recheck recorded neither page. Before page 3 is
        # read the recent keys catch up and #7 recedes from page 3 to page 1.
        with mock.patch.object(watch, "PAGE_SIZE", 3):
            rows = [issue(n) for n in range(1, 8)]
            for n, row in enumerate(rows, start=1):
                row["key"] = row["updated_at"] = watch.iso(NOW - 2000 + n)
            for row in rows[1:6]:  # #2..#6 are still live
                row["updated_at"] = watch.iso(NOW - 1)
            api = LagApi(rows, [])
            get = api.get
            mutated = []

            def mutate(path, **kw):
                if "page=3" in path and not mutated:
                    mutated.append(True)
                    for row in rows[1:6]:
                        row["key"] = row["updated_at"]
                return get(path, **kw)

            with mock.patch.object(api, "get", side_effect=mutate):
                events, until = self.finish(api, NOW - 3600, NOW)
        self.assertEqual({e["object_id"] for e in events}, {1, 7})
        self.assertEqual(until, NOW - watch.INDEX_LAG)

    def test_b11_comment_stream_recovers_the_pushed_row(self):
        # B11 #2955: same receding row in the comment stream.
        with mock.patch.object(watch, "PAGE_SIZE", 3):
            rows = [comment(n) for n in range(10, 17)]
            for n, row in enumerate(rows, start=10):
                row["key"] = row["updated_at"] = watch.iso(NOW - 2000 + n)
            for row in rows[1:6]:
                row["updated_at"] = watch.iso(NOW - 1)
            api = LagApi(comments=rows)
            get = api.get
            mutated = []

            def mutate(path, **kw):
                if "page=3" in path and not mutated:
                    mutated.append(True)
                    for row in rows[1:6]:
                        row["key"] = row["updated_at"]
                return get(path, **kw)

            with mock.patch.object(api, "get", side_effect=mutate):
                events, until = self.finish(api, NOW - 3600, NOW)
        self.assertEqual({e["object_id"] for e in events}, {10, 16})
        self.assertEqual(until, NOW - watch.INDEX_LAG)

    def test_b11_real_page_size_recovers_the_row_pushed_into_the_mixed_page(self):
        # B11 #2955 at the real page size: #201 (settled) recedes from page 3
        # to page 1 when the keys of #2..#200 catch up before page 3 is read.
        with mock.patch.object(watch, "PAGE_SIZE", 100):
            rows = [issue(n) for n in range(1, 202)]
            for n, row in enumerate(rows, start=1):
                row["key"] = row["updated_at"] = watch.iso(NOW - 2500 + n)
            for row in rows[1:200]:
                row["updated_at"] = watch.iso(NOW - 1)
            api = LagApi(rows, [])
            get = api.get
            mutated = []

            def mutate(path, **kw):
                if "page=3" in path and not mutated:
                    mutated.append(True)
                    for row in rows[1:200]:
                        row["key"] = row["updated_at"]
                return get(path, **kw)

            with mock.patch.object(api, "get", side_effect=mutate):
                events, until = self.finish(api, NOW - 3600, NOW)
        self.assertEqual({e["object_id"] for e in events}, {1, 201})
        self.assertEqual(until, NOW - watch.INDEX_LAG)

    def test_b11_page_traversed_with_an_anchor_is_rechecked(self):
        # B11 #2955: page 2 is full of recent rows and is traversed with an
        # anchor, so it was never recorded; #10 recedes from page 4 to page 2
        # before page 4 is read.
        with mock.patch.object(watch, "PAGE_SIZE", 3):
            rows = [issue(n) for n in range(1, 11)]
            for n, row in enumerate(rows, start=1):
                row["key"] = row["updated_at"] = watch.iso(NOW - 2000 + n)
            for row in rows[3:9]:  # #4..#9 are still live
                row["updated_at"] = watch.iso(NOW - 1)
            api = LagApi(rows, [])
            get = api.get
            mutated = []

            def mutate(path, **kw):
                if "page=4" in path and not mutated:
                    mutated.append(True)
                    for row in rows[3:9]:
                        row["key"] = row["updated_at"]
                return get(path, **kw)

            with mock.patch.object(api, "get", side_effect=mutate):
                events, until = self.finish(api, NOW - 3600, NOW)
        self.assertEqual({e["object_id"] for e in events}, {1, 2, 3, 10})
        self.assertEqual(until, NOW - watch.INDEX_LAG)

    def test_b11_recheck_covers_a_page_that_has_no_recent_row(self):
        # B11 #2955: #13 (settled) is pushed from page 5 into page 3, an
        # all-settled page read before the shift. Recording only pages that
        # hold a deferred row would miss it; the recheck starts at the first
        # page that held one (page 2) and covers every page up to the last
        # page read.
        with mock.patch.object(watch, "PAGE_SIZE", 3):
            rows = [issue(n) for n in range(1, 14)]
            for n, row in enumerate(rows, start=1):
                row["key"] = row["updated_at"] = watch.iso(NOW - 2000 + n)
            live = (4, 5, 6, 10, 11, 12)
            for n in live:
                rows[n - 1]["updated_at"] = watch.iso(NOW - 1)
            api = LagApi(rows, [])
            get = api.get
            mutated = []

            def mutate(path, **kw):
                if "page=5" in path and not mutated:
                    mutated.append(True)
                    for n in live:
                        rows[n - 1]["key"] = rows[n - 1]["updated_at"]
                return get(path, **kw)

            with mock.patch.object(api, "get", side_effect=mutate):
                events, until = self.finish(api, NOW - 3600, NOW)
        self.assertEqual({e["object_id"] for e in events},
                         {1, 2, 3, 7, 8, 9, 13})
        self.assertEqual(until, NOW - watch.INDEX_LAG)

    def test_b11_anchor_page_becoming_mixed_is_rechecked(self):
        # B11 #2955 (cx2): page 1 is settled when read; before page 2 is read,
        # #2..#6 are edited (still deferred, keys lagging), so the anchor page
        # becomes mixed. It is only re-read as `previous`, which used to leave
        # the recheck floor at 2. #7 then recedes into page 1 and was lost.
        for kind in ("issue", "comment"):
            with self.subTest(kind=kind):
                with mock.patch.object(watch, "PAGE_SIZE", 3):
                    rows = ([issue(n) for n in range(1, 8)] if kind == "issue"
                            else [comment(n) for n in range(10, 17)])
                    ids = [row["id"] for row in rows]
                    for n, row in enumerate(rows, start=1):
                        row["key"] = row["updated_at"] = watch.iso(NOW - 2500 + n)
                    api = (LagApi(rows, []) if kind == "issue"
                           else LagApi(comments=rows))
                    get = api.get
                    edited, caught = [], []

                    def mutate(path, **kw):
                        page = int(parse_qs(urlsplit(path).query)["page"][0])
                        if page == 2 and not edited:
                            edited.append(True)
                            for row in rows[1:6]:
                                row["updated_at"] = watch.iso(NOW + 1)
                        elif page == 3 and not caught:
                            caught.append(True)
                            for row in rows[1:6]:
                                row["key"] = row["updated_at"]
                        return get(path, **kw)

                    with mock.patch.object(api, "get", side_effect=mutate):
                        events, until = self.finish(api, NOW - 3600, NOW)
                self.assertEqual({e["object_id"] for e in events},
                                 {ids[0], ids[1], ids[2], ids[6]})
                self.assertEqual(until, NOW - watch.INDEX_LAG)

    def test_b11_real_page_size_anchor_page_becoming_mixed(self):
        # B11 #2955 at the real page size: page 1 is settled when read, then
        # #2..#200 are edited before page 2; the anchor page becomes mixed and
        # must enter the recheck range, or #201 is lost.
        with mock.patch.object(watch, "PAGE_SIZE", 100):
            rows = [issue(n) for n in range(1, 202)]
            for n, row in enumerate(rows, start=1):
                row["key"] = row["updated_at"] = watch.iso(NOW - 2500 + n)
            api = LagApi(rows, [])
            get = api.get
            edited, caught = [], []

            def mutate(path, **kw):
                page = int(parse_qs(urlsplit(path).query)["page"][0])
                if page == 2 and not edited:
                    edited.append(True)
                    for row in rows[1:200]:
                        row["updated_at"] = watch.iso(NOW + 1)
                elif page == 3 and not caught:
                    caught.append(True)
                    for row in rows[1:200]:
                        row["key"] = row["updated_at"]
                return get(path, **kw)

            with mock.patch.object(api, "get", side_effect=mutate):
                events, until = self.finish(api, NOW - 3600, NOW)
        self.assertEqual({e["object_id"] for e in events},
                         set(range(1, 101)) | {201})
        self.assertEqual(until, NOW - watch.INDEX_LAG)

    def test_3096_clean_page_edited_after_its_last_read_is_rechecked(self):
        # #3096 (review of #2955): page 1 is clean at its read and at its
        # re-read as the anchor of page 2; page 2 is all deferred, so its
        # snapshot is empty. #1 is then edited and caught up with #3/#4: #5
        # recedes from page 3 into page 1, below the first live page (2).
        for kind in ("issue", "comment"):
            with self.subTest(kind=kind):
                with mock.patch.object(watch, "PAGE_SIZE", 2):
                    rows = ([issue(n) for n in range(1, 6)] if kind == "issue"
                            else [comment(n) for n in range(10, 15)])
                    ids = [row["id"] for row in rows]
                    for n, row in enumerate(rows, start=1):
                        row["key"] = row["updated_at"] = watch.iso(NOW - 2500 + n)
                    for row in rows[2:4]:
                        row["updated_at"] = watch.iso(NOW - 1)
                    api = (LagApi(rows, []) if kind == "issue"
                           else LagApi(comments=rows))
                    get = api.get
                    caught = []

                    def mutate(path, **kw):
                        page = int(parse_qs(urlsplit(path).query)["page"][0])
                        if page == 3 and not caught:
                            caught.append(True)
                            rows[0]["updated_at"] = watch.iso(NOW + 1)
                            for row in (rows[0], rows[2], rows[3]):
                                row["key"] = row["updated_at"]
                        return get(path, **kw)

                    with mock.patch.object(api, "get", side_effect=mutate):
                        events, until = self.finish(api, NOW - 3600, NOW)
                self.assertEqual({e["object_id"] for e in events},
                                 {ids[0], ids[1], ids[4]})
                self.assertEqual(until, NOW - watch.INDEX_LAG)

    def test_3096_pending_legacy_recheck_also_reaches_page_one(self):
        for kind in ("issue", "comment"):
            for pending in ({"from": 2, "to": 3, "index": 0},
                            {"from": 2, "to": 3, "index": 2},
                            {"pages": [2, 3], "index": 1}):
                with self.subTest(kind=kind, pending=pending):
                    make = issue if kind == "issue" else comment
                    rows = [make(n) for n in (2, 5, 3, 4, 1)]
                    for row in rows[2:]:
                        row["updated_at"] = watch.iso(NOW - 1)
                    scan = dict(since=NOW - 3600, window_since=NOW - 3600,
                                until=NOW, settled=NOW - watch.INDEX_LAG,
                                kind=kind, page=3, recheck_from=2, recheck_to=3,
                                recheck=dict(pending, then=("comments"
                                             if kind == "issue" else "done")))
                    before = deepcopy(scan)
                    api = (Api(rows) if kind == "issue" else Api(comments=rows))
                    with mock.patch.object(watch, "PAGE_SIZE", 2), \
                            mock.patch.object(watch, "PAGES_PER_TICK", 1):
                        events, until = self.finish(api, NOW - 3600, NOW, scan)
                    self.assertIn(5, {event["object_id"] for event in events})
                    self.assertEqual(until, NOW - watch.INDEX_LAG)
                    self.assertEqual(scan, before)

    def test_b6_anchor_must_be_an_accepted_row(self):
        # B6 #2955: #6 is deferred (> U) but its old key sits at the end of the
        # page. When #1..#5 move and #6 catches up its key, the re-read page is
        # [6,1,2]. An anchor taken from #6 (never accepted) would hide the
        # multi-page shift and lose #7..#9. The snapshot keeps settled rows
        # only, so the scan restarts and delivers them.
        with mock.patch.object(watch, "PAGE_SIZE", 3):
            rows = [issue(n) for n in range(1, 10)]
            for n, row in enumerate(rows):
                row["key"] = row["updated_at"] = watch.iso(NOW - 1000 + n)
            rows[5]["updated_at"] = watch.iso(NOW - 250)  # key still -995
            api = LagApi(rows, [])
            events, scan, until = watch.collect_batch(
                api, SUB, NOW - 3600, NOW, None)
            more, scan, _ = watch.collect_batch(api, SUB, NOW, NOW, scan)
            events += more
            self.assertEqual({e["object_id"] for e in events}, set(range(1, 6)))
            self.assertEqual(scan["page"], 3)
            self.assertNotIn(6, [stamp[0] for stamp in scan["snap_prev"]])
            for row in rows[:5]:
                row["key"] = row["updated_at"] = watch.iso(NOW + 1)
            rows[5]["key"] = watch.iso(NOW - 250)  # key catches up, stamp same
            more, _ = self.finish(api, NOW, NOW, scan)
            events += more
            self.assertEqual({e["object_id"] for e in events},
                             set(range(1, 10)) - {6})
            more, _ = self.finish(api, until - watch.OVERLAP, NOW + 400, None)
            events += more
            self.assertEqual({e["object_id"] for e in events}, set(range(1, 10)))

    def test_index_lag_with_latency_inside_the_bound_is_deferred_then_read(self):
        # A row written 250 s before the scan is still inside the 300 s latency
        # window: it is ignored, then delivered by the next window when its key
        # has caught up. The lagging successor #4 is not skipped.
        with mock.patch.object(watch, "PAGE_SIZE", 3):
            rows = [issue(n) for n in range(1, 5)]
            for row, key, updated in zip(rows, (-1000, -900, -700, -640),
                                         (-1000, -900, -250, -640)):
                row["key"] = watch.iso(NOW + key)
                row["updated_at"] = watch.iso(NOW + updated)
            api = LagApi(rows, [])
            first, until = self.finish(api, NOW - 3600, NOW)
            self.assertEqual({e["object_id"] for e in first}, {1, 2, 4})
            rows[2]["key"] = watch.iso(NOW - 250)
            more, _ = self.finish(api, until - watch.OVERLAP,
                                  NOW + watch.INDEX_LAG + 1)
            self.assertEqual({e["object_id"] for e in first + more}, {1, 2, 3, 4})

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
                    batch, scan, until = watch.collect_batch(api, SUB, NOW - 3600, NOW, scan)
                    events += batch
                    if scan is None:
                        break
                self.assertIsNone(scan)
                self.assertEqual({e["object_id"] for e in events if e["kind"] == "issue"},
                                 set(range(2, size + 1)))
                del rows[0]["key"]  # the index caught up
                events, _, _ = watch.collect_batch(
                    api, SUB, until - watch.OVERLAP, NOW + 400, None)
                self.assertEqual([e["object_id"] for e in events if e["kind"] == "issue"], [1])

    def test_edit_on_the_delivered_page_is_deferred_then_delivered(self):
        # #2955: an edit moves updated_at after U. It is not smuggled into the
        # current scan; the next window delivers the new version.
        with mock.patch.object(watch, "PAGE_SIZE", 2):
            api = UpdatedApi([issue(n) for n in range(1, 6)])
            events, scan, until = watch.collect_batch(api, SUB, NOW - 3600, NOW, None)
            more, scan, _ = watch.collect_batch(api, SUB, NOW, NOW, scan)
            events += more
            self.assertEqual({e["object_id"] for e in events}, {1, 2, 3, 4})
            api.issues[2]["body"] = "Edited"
            api.issues[2]["updated_at"] = watch.iso(NOW - 1)
            batch, _, _ = watch.collect_batch(api, SUB, NOW, NOW, scan)
            self.assertNotIn(3, {e["object_id"] for e in batch})
            self.assertIn(5, {e["object_id"] for e in batch})
            batch, _ = self.finish(api, until - watch.OVERLAP,
                                   NOW + watch.INDEX_LAG + 1)
            self.assertIn((3, "Edited"),
                          [(e["object_id"], e["body"]) for e in batch])

    def test_legacy_checkpoint_replays_under_the_bounded_window(self):
        # A checkpoint from before #2955 has no ``settled`` bound. It must
        # replay page 1 rather than trust the old shift snapshots.
        rows = [issue(n) for n in range(1, 7)]
        for n, row in enumerate(rows):
            row["updated_at"] = watch.iso(NOW - 1000 + n)
        api = UpdatedApi(rows, [])
        legacy = dict(since=NOW - 3600, window_since=NOW - 3600, until=NOW,
                      kind="issue", page=3,
                      snap_prev=[[3, rows[2]["updated_at"]], [4, rows[3]["updated_at"]]],
                      snap_cur=[[5, rows[4]["updated_at"]]])
        with mock.patch.object(watch, "PAGE_SIZE", 2):
            events, scan, _ = watch.collect_batch(api, SUB, NOW - 3600, NOW, legacy)
            self.assertEqual({e["object_id"] for e in events}, {1, 2})
            self.assertEqual(scan["page"], 2)
            self.assertNotIn("snap_cur", scan)
            more, _ = self.finish(api, NOW, NOW, scan)
        self.assertEqual({e["object_id"] for e in events + more}, set(range(1, 7)))

    def test_disorder_inside_the_window_fails_closed(self):
        inside = [issue(n) for n in range(1, 4)]
        for n, row in enumerate(inside):
            row["updated_at"] = watch.iso(NOW - 1000 - n)
        with self.assertRaisesRegex(plan_github.GithubError, "updated order is invalid"):
            watch.collect_batch(Api(inside), SUB, NOW - 3600, NOW, None)

    def test_latency_beyond_the_bound_is_outside_the_model(self):
        # Contract #2955: a row stamped inside the settled window is assumed to
        # have its final index place (latency <= INDEX_LAG). A key lagging by
        # 1500 s violates that hypothesis; the order guard fails closed rather
        # than silently skipping the row. Such a lag needs a larger INDEX_LAG,
        # not a hidden fallback (docs/EXPLOITATION.md).
        rows = [issue(n) for n in range(1, 4)]
        for row, key, updated in zip(rows, (-1000, -2000, -900),
                                     (-1000, -500, -400)):
            row["key"] = watch.iso(NOW + key)
            row["updated_at"] = watch.iso(NOW + updated)
        with self.assertRaisesRegex(plan_github.GithubError, "updated order is invalid"):
            watch.collect_batch(LagApi(rows, []), SUB, NOW - 3600, NOW, None)

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
        self.assertEqual(len(api.calls), 1)  # page 1; the next pass reads its predecessor
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

    def test_index_lag_is_configurable_and_bounded(self):
        fiche = NS(data={"github_watch": {"app": {
            "repos": ["acme/app"], "index_lag": 600}}})
        self.assertEqual(watch.subscriptions([self.bundle(fiche=fiche)]),
                         [dataclasses.replace(SUB, index_lag=600)])
        for lag in (True, -1, 3601, float("nan"), "300"):
            warnings = []
            fiche = NS(data={"github_watch": {
                "app": {"repos": ["acme/app"], "index_lag": lag}}})
            with self.subTest(lag=lag):
                self.assertEqual(watch.subscriptions([self.bundle(fiche=fiche)],
                                                     log=warnings.append), [])
                self.assertIn("index_lag", warnings[0])

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
        self.assertEqual(storage.of(self.db).github_watch.cursor("app", "acme/app"),
                         NOW + 1802 - watch.INDEX_LAG)

    def test_latency_window_defers_then_delivers_on_the_next_tick(self):
        # #2955: a write inside INDEX_LAG is ignored by the first window; the
        # committed cursor stops at the settled bound U, so the next tick
        # starts OVERLAP before U and delivers the row.
        api = UpdatedApi(issues=[dict(issue(), updated_at=watch.iso(NOW - 100))])
        watcher = self.watcher(api)
        self.assertEqual(watcher.tick(self.db, NOW), [dict(project="app",
                                                          repo="acme/app", events=0)])
        self.assertEqual(self.messages(), [])
        self.assertEqual(storage.of(self.db).github_watch.cursor("app", "acme/app"),
                         NOW - watch.INDEX_LAG)
        self.assertEqual(watcher.tick(self.db, NOW + 901)[0]["events"], 1)
        self.assertEqual(len(self.messages()), 1)
        self.assertIn("acme/app#1", self.messages()[0]["body"])

    def test_long_settled_scan_alerts_and_holds_the_cursor(self):
        # #2955: a scan open more than STALLED_TICKS logs
        # `github-watch-stalled`, keeps the cursor until it really ends, then
        # commits U. This is the progressive long-scan case; no page offset is
        # trusted across a key catch-up.
        api = Api([issue(n) for n in range(1, 32)])
        logs = []
        watcher = self.watcher(api, log=logs.append, stalled_ticks=6)
        with mock.patch.object(watch, "PAGE_SIZE", 3), \
                mock.patch.object(watch, "DIGEST_MAX_ITEMS", 100):
            for _ in range(7):
                watcher.tick(self.db, NOW)
            self.assertIsNotNone(storage.of(self.db).github_watch.scan(
                "app", "acme/app"))
            self.assertEqual(storage.of(self.db).github_watch.cursor(
                "app", "acme/app"), NOW - watch.INDEX_LAG - watch.BOOTSTRAP)
            self.assertTrue(any("github-watch-stalled" in line for line in logs))
            for _ in range(10):
                watcher.tick(self.db, NOW)
                if storage.of(self.db).github_watch.scan("app", "acme/app") is None:
                    break
        self.assertIsNone(storage.of(self.db).github_watch.scan("app", "acme/app"))
        self.assertEqual(storage.of(self.db).github_watch.cursor(
            "app", "acme/app"), NOW - watch.INDEX_LAG)

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
                             NOW - watch.INDEX_LAG - watch.BOOTSTRAP)
            # A leftover digest continues on the next notifier tick, without
            # waiting for the repository cadence.
            watcher.tick(self.db, NOW)
            messages = self.messages()
            self.assertEqual(len(messages), 2)
            self.assertIn("2 input(s)", messages[-1]["body"])
            self.assertEqual(storage.of(self.db).github_watch.cursor("app", "acme/app"),
                             NOW - watch.INDEX_LAG)
            watcher.tick(self.db, NOW + 901)
            self.assertEqual(len(self.messages()), 2)
            self.assertEqual(storage.of(self.db).github_watch.cursor("app", "acme/app"),
                             NOW + 901 - watch.INDEX_LAG)

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
                                 NOW - watch.INDEX_LAG - watch.BOOTSTRAP)
            self.assertEqual(watch.deliver(
                self.db, SUB, events, until, [], self.cfg.host, partial=True,
                scan_before=None, scan_after=progress), 1)
        self.assertEqual(storage.of(self.db).github_watch.scan("app", "acme/app"), progress)
        self.assertEqual(storage.of(self.db).github_watch.cursor("app", "acme/app"),
                         NOW - watch.INDEX_LAG - watch.BOOTSTRAP)
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
        self.assertEqual(storage.of(self.db).github_watch.cursor("app", "acme/app"),
                         NOW - watch.INDEX_LAG)
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
        self.assertEqual(storage.of(self.db).github_watch.cursor("app", "acme/app"),
                         NOW + 901 - watch.INDEX_LAG)

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
        self.assertEqual(storage.of(self.db).github_watch.cursor("app", "acme/app"),
                         NOW - watch.INDEX_LAG)
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
        self.assertEqual(storage.of(self.db).github_watch.cursor("app", "acme/app"),
                         NOW - watch.INDEX_LAG)

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
        self.assertEqual(storage.of(self.db).github_watch.cursor("app", "acme/app"),
                         NOW - watch.INDEX_LAG)
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
