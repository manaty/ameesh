# SPDX-License-Identifier: AGPL-3.0-only
"""#5858: a git work tree left out of the tmp quota eviction is signaled when
it appears, then at most every 12 h per path.

Measured on ameesh-agents-1: 25 025 `tmp signaled` lines in 24 h, one written
at every turn end and every host pass for the same worktree, drowning the
housekeeping journal and the orphan reading. `eviction_entries` now reads the
last journal line of the path and stays silent while it is a recent
`signaled` (same sticky rule as the /tmp entries of #222).
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

from ameesh import housekeeping as menage
from ameesh import storage

from .support import PgTestCase
from .test_l73_housekeeping import _Cfg, _git

Q = 4 * 1024 ** 3


class _Journal:
    """The last `tmp` line of each path, as `latest_by_path(path=…)` reads it."""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.housekeeping = mock.Mock()
        self.housekeeping.latest_by_path.side_effect = self._latest

    def _latest(self, host, kind, *, path=None, **kw):
        row = self.rows.get(path)
        return [row] if row else []

    def write(self, entries, at_ts: float) -> None:
        """What the pass does with the lines: `housekeeping.log` writes them."""
        for entry in entries:
            if entry.get("kind") == "tmp":
                self.rows[entry["path"]] = dict(entry, at_ts=at_ts)


def _result(tree: str, *, symlink: str | None = None) -> dict:
    return {"before": 0, "after": 0, "evicted": 0, "files": 0,
            "over_quota": False, "unremovable": 0, "worktrees": [tree],
            "worktree_symlinks": [symlink] if symlink else [],
            "truncated": False, "truncated_paths": [], "failed": [],
            "blocked": False, "mounts_unreadable": False, "budget_reached": False}


class WorktreeSignalTest(unittest.TestCase):

    def setUp(self) -> None:
        self.journal = _Journal()
        patcher = mock.patch.object(menage.storage, "of", return_value=self.journal)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _entries(self, result, now: float) -> list[dict]:
        entries = menage.eviction_entries(
            result, kind="tmp", path="/tmp/a1", quota=Q, db=object(), host="pc",
            now=now)
        self.journal.write(entries, now)
        return entries

    @staticmethod
    def _signaled(entries: list[dict]) -> list[dict]:
        return [e for e in entries if e["action"] == "signaled"]

    def test_deux_passages_sur_le_meme_worktree_une_seule_ligne(self):
        tree = "/tmp/a1/review-1"
        result = _result(tree)
        first = self._signaled(self._entries(result, 1000.0))
        second = self._signaled(self._entries(result, 1060.0))
        self.assertEqual([e["path"] for e in first], [tree])
        self.assertEqual(second, [])

    def test_re_signale_apres_douze_heures_avec_la_premiere_observation(self):
        tree = "/tmp/a1/review-1"
        result = _result(tree)
        first = self._signaled(self._entries(result, 1000.0))
        before = self._signaled(self._entries(
            result, 1000.0 + menage.WORKTREE_SIGNAL_REFRESH_S - 1))
        after = self._signaled(self._entries(
            result, 1000.0 + menage.WORKTREE_SIGNAL_REFRESH_S + 1))
        self.assertEqual((len(first), before, [e["path"] for e in after]),
                         (1, [], [tree]))
        self.assertEqual(after[0]["data"]["first_seen"], 1000.0)

    def test_marqueur_git_symlink_suit_la_meme_regle(self):
        tree, link = "/tmp/a1/review-1", "/tmp/a1/lien"
        result = _result(tree, symlink=link)
        first = self._signaled(self._entries(result, 1000.0))
        second = self._signaled(self._entries(result, 1060.0))
        self.assertEqual([e["path"] for e in first], [tree, link])
        self.assertEqual(second, [])

    def test_journal_illisible_signale_comme_avant(self):
        self.journal.housekeeping.latest_by_path.side_effect = RuntimeError("db")
        result = _result("/tmp/a1/review-1")
        first = self._signaled(self._entries(result, 1000.0))
        second = self._signaled(self._entries(result, 1060.0))
        self.assertEqual((len(first), len(second)), (1, 1))

    def test_sans_base_signale_comme_avant(self):
        result = _result("/tmp/a1/review-1")
        entries = menage.eviction_entries(result, kind="tmp", path="/tmp/a1",
                                          quota=Q, now=1000.0)
        self.assertEqual([e["path"] for e in self._signaled(entries)],
                         ["/tmp/a1/review-1"])


class WorktreeSignalDbTest(PgTestCase):
    """The real turn-end path: one journal line for the same worktree, not one
    per turn end."""

    def setUp(self) -> None:
        super().setUp()
        for table in ("housekeeping_log", "managed_worktrees"):
            self.db.execute("DELETE FROM %s" % table)
        self.cfgl = _Cfg(self.state)
        self.pol = menage.Policy(orphan_min_size=1)

    def _end(self, turn_id: str) -> list[dict]:
        turn = menage.Turn.begin(self.cfgl, self.pol, "a1", None,
                                 new_session=False, base_env={})
        entries = turn.end(lot=None, turn_id=turn_id, db=self.db, host="pc")
        storage.of(self.db).housekeeping.log("pc", entries, actor="test")
        return entries

    def test_deux_fins_de_tour_sur_le_meme_worktree_une_seule_ligne(self):
        first = menage.Turn.begin(self.cfgl, self.pol, "a1", None,
                                  new_session=True, base_env={})
        repo = os.path.join(first.tmp, "review-1")
        _git("init", "-q", "-b", "main", repo, cwd=first.tmp)
        entries = first.end(lot=None, turn_id="t" * 32, db=self.db, host="pc")
        storage.of(self.db).housekeeping.log("pc", entries, actor="test")
        entries += self._end("u" * 32)
        signaled = [e for e in entries if e["kind"] == "tmp"
                    and e["action"] == "signaled" and e["path"] == repo]
        self.assertEqual(len(signaled), 1)
        rows = storage.of(self.db).housekeeping.recent("pc", 3600, kind="tmp")
        self.assertEqual(len([r for r in rows if r["path"] == repo
                              and r["action"] == "signaled"]), 1)


if __name__ == "__main__":
    unittest.main()
