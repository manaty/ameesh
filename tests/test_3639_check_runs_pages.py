# SPDX-License-Identifier: AGPL-3.0-only
"""#3639 — les check-runs d'un commit se lisent page par page.

Une tête de main qui porte la suite complète dépasse 100 check-runs (141 sur
fb8b7403). Une seule page la rendait illisible (« malformed or truncated
response »), et la coupe release ne lisait plus la CI de main.
"""
from __future__ import annotations

import json
import unittest

from ameesh import plan_github, radar

SHA = "f" * 40
SLUG = "manaty/ameesh"


def _check(n: int) -> dict:
    return {"id": n, "name": "part %d" % n, "head_sha": SHA, "app": {"id": 15368},
            "status": "completed", "conclusion": "success",
            "check_suite": {"id": 7}}


class Gh:
    """Rend `total` check-runs en pages de 100, comme l'API REST."""

    def __init__(self, total: int, *, shown=None, drift=False, short_page=None):
        self.total = total
        self.shown = total if shown is None else shown
        self.drift = drift
        self.short_page = short_page
        self.paths = []

    def run(self, argv, stdin=None):
        path = argv[-1]
        self.paths.append(path)
        page = int(path.rsplit("&page=", 1)[1]) if "&page=" in path else 1
        start = (page - 1) * radar.CHECK_RUNS_PAGE
        stop = min(start + radar.CHECK_RUNS_PAGE, self.shown)
        if self.short_page == page:
            stop = start + 10
        runs = [_check(n) for n in range(start, max(start, stop))]
        total = self.total + (page - 1 if self.drift else 0)
        return 0, json.dumps({"total_count": total, "check_runs": runs}), ""


class CommitCheckRunsTest(unittest.TestCase):
    def test_a_commit_with_141_check_runs_is_read_in_two_pages(self):
        gh = Gh(141)
        runs = radar.commit_check_runs(gh, SLUG, SHA)
        self.assertEqual(len(runs), 141)
        self.assertEqual([r["id"] for r in runs], list(range(141)))
        self.assertEqual(len(gh.paths), 2)
        # la première page garde l'URL d'avant #3639
        self.assertEqual(gh.paths[0], "repos/%s/commits/%s/check-runs?filter=all&per_page=100"
                         % (SLUG, SHA))
        self.assertTrue(gh.paths[1].endswith("&page=2"))

    def test_a_single_page_is_read_once(self):
        gh = Gh(84)
        self.assertEqual(len(radar.commit_check_runs(gh, SLUG, SHA)), 84)
        self.assertEqual(len(gh.paths), 1)

    def test_exactly_one_full_page_stops_at_the_total(self):
        gh = Gh(100)
        self.assertEqual(len(radar.commit_check_runs(gh, SLUG, SHA)), 100)
        self.assertEqual(len(gh.paths), 1)

    def test_no_check_run_is_an_empty_list(self):
        self.assertEqual(radar.commit_check_runs(Gh(0), SLUG, SHA), [])

    def test_a_missing_page_still_raises(self):
        # la page 2 rend 10 lignes au lieu de 41 : le compte ne tombe pas juste
        with self.assertRaises(plan_github.GithubError):
            radar.commit_check_runs(Gh(141, short_page=2), SLUG, SHA)

    def test_a_total_that_changes_while_paging_raises(self):
        with self.assertRaises(plan_github.GithubError):
            radar.commit_check_runs(Gh(141, drift=True), SLUG, SHA)

    def test_fewer_rows_than_the_total_raises(self):
        with self.assertRaises(plan_github.GithubError):
            radar.commit_check_runs(Gh(84, shown=80), SLUG, SHA)

    def test_beyond_the_api_cap_raises(self):
        gh = Gh(1001)
        with self.assertRaises(plan_github.GithubError):
            radar.commit_check_runs(gh, SLUG, SHA)
        self.assertEqual(len(gh.paths), radar.CHECK_RUNS_MAX_PAGES)

    def test_a_malformed_page_raises(self):
        class Bad:
            def run(self, argv, stdin=None):
                return 0, json.dumps({"check_runs": []}), ""

        with self.assertRaises(plan_github.GithubError):
            radar.commit_check_runs(Bad(), SLUG, SHA)


if __name__ == "__main__":
    unittest.main()
