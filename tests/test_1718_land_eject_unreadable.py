# SPDX-License-Identifier: AGPL-3.0-only
"""#1718 — désarmement raté puis `gh pr list --state merged` illisible : la
file éjecte toujours, fail closed.

Sans la garde, la LandError de `pr_merged` remontait au chemin de reprise de
`land` : l'entrée repassait en `queued` (« landing failed … retried ») dans
`_land`, ou restait `landing` dans `_follow_pr`, sans l'éjection #1656 qui
demande de désarmer à la main — l'auto-merge restait armé jusqu'à la passe
suivante.
"""
from __future__ import annotations

import subprocess

from . import test_l160e2_land as _land
from .test_1656_auto_merge import AutoGh


class UnreadableGh(AutoGh):
    """Le désarmement échoue, et la liste des PR fusionnées est illisible."""

    def __call__(self, args, timeout=60.0):
        if args[:2] == ["pr", "list"] and "--state" in args and "merged" in args:
            self.calls.append(args)
            return subprocess.CompletedProcess(args, 1, "", "HTTP 502: bad gateway")
        return super().__call__(args, timeout=timeout)


class EjectUnreadableTest(_land.PrLandTest):
    def assert_ejected(self, out, gh):
        self.assertEqual(out["result"], "ejected", out)
        row = self.fake.queue_rows[4]
        self.assertEqual(row["state"], "ejected")
        self.assertIn("auto-merge armed and not disarmed", row["reason"])
        self.assertIn("merged PRs unreadable", row["reason"])
        self.assertIn("HTTP 502", row["reason"])
        self.assertTrue(any("--disable-auto" in body for _, body, _ in self.sent))
        self.assertFalse(any("--match-head-commit" in c for c in gh.calls))

    def test_landing_ejects_when_the_merged_list_is_unreadable(self):
        gh = UnreadableGh()
        self.queued(4)
        gh.head, gh.auto = self.tip(4), True
        gh.disable_error = "HTTP 403: must have admin rights"
        self.assert_ejected(self.land(4, gh=gh), gh)

    def test_following_ejects_when_the_merged_list_is_unreadable(self):
        gh = UnreadableGh()
        self.queued(4)
        gh.head = self.tip(4)
        first = self.land(4, gh=gh)
        self.assertEqual(first["result"], "landing", first)
        gh.auto, gh.head, gh.ci = True, first["sha"], "pending"
        gh.disable_error = "HTTP 403: must have admin rights"
        self.assert_ejected(self.land(4, gh=gh), gh)
