# SPDX-License-Identifier: AGPL-3.0-only
"""#530: the non-blocking points of the codex r2 verdict on #500, plus #718.

1. the unremovable bytes are classified and reported even when the total is
   under the quota (they read as zero before);
2. a truncated listing keeps its `since` across passes and gets a lifting
   entry, as the `unremovable` episode does (`tmp_unscanned` stayed raised
   until its 24 h expiry even after a clean scan);
3. the bytes of a hard-linked inode are credited when its last name goes: #500
   r6 removes every name the walk saw, this adds the credit rule for a name
   the walk did not see (outside the root, or in a protected subtree);
4. directories consume `WALK_LIMIT` (limit 3 walked 10 001 directories), and
   the pruning is bounded too;
5. #718: an unreadable mount table writes a `mounts_unreadable` journal entry
   and raises `tmp_unscanned`, and it never resolves a previous episode —
   it did not scan, so it proves nothing.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from ameesh import exploitation, menage, platform, registry

from .support import PgTestCase

HOUR = 3600.0
#: one file of one block: its size on disk is its length (sizes count blocks)
B = 4096


class _Cfg:
    def __init__(self, state_dir: str, dsn: str = "", schema: str = "public"):
        self.state_dir, self.dsn, self.schema, self.host = state_dir, dsn, schema, "pc"


def _write(path: str, size: int = B, age_s: float = 3 * HOUR) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"z" * size)
    if age_s:
        at = time.time() - age_s
        os.utime(path, (at, at))


class _Foreign:
    """Files of `paths` look owned by another user (root, in the incident)."""

    def __init__(self, paths: set):
        self.paths = paths

    def __enter__(self):
        real = os.lstat

        class _St:
            def __init__(self, st):
                self._st = st

            def __getattr__(self, name):
                if name == "st_uid":
                    return os.geteuid() + 1
                return getattr(self._st, name)

        def lstat(path, *a, **kw):
            st = real(path, *a, **kw)
            return _St(st) if os.fspath(path) in self.paths else st

        self._patch = mock.patch.object(menage.os, "lstat", lstat)
        self._patch.start()

    def __exit__(self, *exc):
        self._patch.stop()
        return False


class FollowUpTest(unittest.TestCase):
    """Points (1), (3) and (4) at the `evict_lru`/`_files` level, no database."""

    def setUp(self) -> None:
        self.base = tempfile.mkdtemp(prefix="t530-")
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.root = os.path.join(self.base, "tmp")
        menage._ensure_root(self.root)
        self.mine = os.path.join(self.root, "codex3")

    # -- point 1 ----------------------------------------------------------

    def test_unremovable_bytes_are_reported_below_the_quota(self):
        # quota 10 blocks, 3 foreign + 3 ours (6 occupied): nothing to evict,
        # but the foreign blocks are said instead of reading as zero
        foreign = {os.path.join(self.mine, "wine", "f%d" % i) for i in range(3)}
        for path in foreign:
            _write(path)
        ours = [os.path.join(self.mine, "o%d" % i) for i in range(3)]
        for path in ours:
            _write(path)
        with _Foreign(foreign):
            result = menage.evict_lru([self.mine], self.root, 10 * B)
        self.assertEqual(result["files"], 0)
        self.assertEqual(result["unremovable"], 3 * B)
        self.assertFalse(result["blocked"])
        self.assertFalse(result["over_quota"])
        self.assertTrue(all(os.path.exists(path) for path in ours))
        entries = menage.eviction_entries(result, kind="tmp", path=self.mine,
                                          quota=10 * B)
        self.assertEqual([e["action"] for e in entries], ["unremovable"])
        self.assertEqual(entries[0]["data"]["unremovable_bytes"], 3 * B)
        self.assertFalse(entries[0]["data"]["blocked"])
        self.assertIn("still holds", entries[0]["detail"])

    def test_turn_end_reports_unremovable_below_the_quota(self):
        cfg = _Cfg(os.path.join(self.base, "state"))
        pol = menage.Policy(tmp_quota=10 * B)
        turn = menage.Turn.begin(cfg, pol, "codex3", None, new_session=False,
                                 base_env={})
        tmp = turn.env["TMPDIR"]
        foreign = {os.path.join(tmp, "wine", "f%d" % i) for i in range(3)}
        for path in foreign:
            _write(path)
        ours = os.path.join(tmp, "out.txt")
        _write(ours)
        with _Foreign(foreign):
            entries = turn.end(lot="530", turn_id="t")
        self.assertTrue(os.path.exists(ours))
        self.assertEqual([(e["kind"], e["action"]) for e in entries],
                         [("tmp", "unremovable")])
        self.assertFalse(entries[0]["data"]["blocked"])
        self.assertEqual(entries[0]["data"]["unremovable_bytes"], 3 * B)

    # -- point 3 ----------------------------------------------------------

    def test_multi_link_inode_is_credited_once_when_the_last_name_goes(self):
        # #500 r6 keeps every name the walk saw and removes them all before
        # crediting; #530 adds the credit rule: the block leaves `counted` when
        # the LAST name goes (both names of the inode are removed in one pass)
        first = os.path.join(self.mine, "a")
        _write(first, size=5)                    # 5 bytes: one 4096 block
        os.link(first, os.path.join(self.mine, "b"))
        names = [os.path.join(self.mine, name) for name in ("a", "b")]
        result = menage.evict_lru([self.mine], self.root, 0)
        self.assertEqual(result["files"], 1)
        self.assertEqual(result["evicted"], B)
        self.assertEqual(result["after"], 0)
        self.assertFalse(result["over_quota"])
        self.assertFalse(any(os.path.exists(p) for p in names))

    def test_multi_link_inode_kept_alive_outside_the_root_credits_nothing(self):
        # #530: every name the walk saw is gone, but a link outside the managed
        # root still holds the inode: the block is not freed, so it stays
        # counted and is reported as unremovable instead of being credited.
        inside = os.path.join(self.mine, "a")
        _write(inside, size=5)
        keep = os.path.join(self.base, "keep")
        os.link(inside, keep)                    # a link outside the eviction
        result = menage.evict_lru([self.mine], self.root, 0)
        self.assertEqual(result["files"], 1)
        self.assertEqual(result["evicted"], 0)
        self.assertEqual(result["after"], B)
        self.assertEqual(result["unremovable"], B)
        self.assertTrue(result["blocked"])
        self.assertFalse(os.path.exists(inside))
        self.assertTrue(os.path.exists(keep))
        entries = menage.eviction_entries(result, kind="tmp", path=self.mine,
                                          quota=0)
        self.assertEqual(entries[-1]["action"], "unremovable")
        self.assertTrue(entries[-1]["data"]["blocked"])

    def test_multi_link_name_hidden_in_a_protected_subtree_credits_nothing(self):
        # #530: a name inside a `.git`-protected subtree is never walked; the
        # candidate is removed but the inode survives, so its bytes stay
        # counted (the P3 residual of the r6 accounting).
        repo = os.path.join(self.mine, "repo")
        os.makedirs(os.path.join(repo, ".git"))
        inside = os.path.join(self.mine, "a")
        _write(inside, size=5, age_s=10 * HOUR)
        os.link(inside, os.path.join(repo, "b"))
        newer = os.path.join(self.mine, "newer")
        _write(newer, size=5, age_s=9 * HOUR)
        result = menage.evict_lru([self.mine], self.root, B)
        self.assertEqual(result["files"], 1)         # `a` was unlinked
        self.assertEqual(result["evicted"], 0)       # its inode is still held
        self.assertEqual(result["unremovable"], B)
        self.assertFalse(os.path.exists(inside))
        self.assertTrue(os.path.exists(os.path.join(repo, "b")))
        self.assertTrue(os.path.exists(newer),
                        "a newer file was evicted to make up for bytes still held")

    # -- point 4 ----------------------------------------------------------

    def test_directories_consume_the_walk_limit(self):
        # 5 directories under the root, no file: with the old counting the
        # walk was complete (limit 3 never reached), it is truncated now
        deep = os.path.join(self.mine, "deep")
        for i in range(5):
            os.makedirs(os.path.join(deep, "d%d" % i))
        state: dict = {}
        with mock.patch.object(menage, "WALK_LIMIT", 3):
            found = list(menage._files(self.mine, state=state))
        self.assertEqual(found, [])
        self.assertTrue(state.get("truncated"))
        self.assertEqual(state.get("truncated_paths"), [self.mine])
        state = {}
        with mock.patch.object(menage, "WALK_LIMIT", 20):
            found = list(menage._files(self.mine, state=state))
        self.assertEqual(found, [])
        self.assertFalse(state.get("truncated"))

    def test_directory_tree_truncates_and_evicts_nothing(self):
        deep = os.path.join(self.mine, "deep")
        for i in range(5):
            os.makedirs(os.path.join(deep, "d%d" % i))
        loose = os.path.join(self.mine, "loose")
        _write(loose)
        with mock.patch.object(menage, "WALK_LIMIT", 3):
            result = menage.evict_lru([self.mine], self.root, 0)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["files"], 0)
        self.assertTrue(os.path.exists(loose))

    def test_prune_stops_at_its_directory_budget(self):
        top = os.path.join(self.base, "prune")
        for i in range(5):
            os.makedirs(os.path.join(top, "d%d" % i))
        real = os.scandir
        opened: list = []

        def counting(path):
            opened.append(os.fspath(path))
            return real(path)

        with mock.patch.object(menage.os, "scandir", counting):
            menage._prune_empty_dirs(top, os.lstat(top).st_dev)
        self.assertEqual(len(opened), 6)          # the root and five children
        self.assertFalse(os.path.exists(os.path.join(top, "d0")))
        for i in range(5):
            os.makedirs(os.path.join(top, "d%d" % i))
        del opened[:]
        with mock.patch.object(menage.os, "scandir", counting):
            menage._prune_empty_dirs(top, os.lstat(top).st_dev, limit=2)
        self.assertLessEqual(len(opened), 2)      # the budget bounds the walk


class EpisodeTest(PgTestCase):
    """Points (2) and (5): the journal episodes and their alerts."""

    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM housekeeping_log")
        self.mine = _Cfg(self.state, self.cfg.dsn, self.cfg.schema)
        self.pol = menage.Policy(tmp_quota=4 * B)
        registry.upsert(self.db, "codex3", harness="codex", host="pc")

    def _alerts(self, kind: str) -> list[dict]:
        return [a for a in exploitation._housekeeping_alerts([], self.db)
                if a["type"] == kind]

    def test_truncated_episode_keeps_its_since_and_is_lifted(self):
        tmp = menage.turn_env(self.mine, self.pol, "codex3", {})["TMPDIR"]
        many = [os.path.join(tmp, "many", "f%d" % i) for i in range(6)]
        for path in many:
            _write(path)
        # l'horloge est menée (`now`), pas attendue : la deuxième passe a un
        # autre instant, donc si `since` était recalculé l'assertion le verrait
        first_now = time.time()
        with mock.patch.object(menage, "WALK_LIMIT", 3):
            menage.run_pass(self.mine, self.db, self.pol, host="pc", actor="t",
                            now=first_now)
        first = self._alerts("tmp_unscanned")
        self.assertEqual(len(first), 1)
        with mock.patch.object(menage, "WALK_LIMIT", 3):
            menage.run_pass(self.mine, self.db, self.pol, host="pc", actor="t",
                            now=first_now + 2.0)
        second = self._alerts("tmp_unscanned")
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0]["since"], first[0]["since"])
        self.assertEqual(exploitation.alert_key(second[0]),
                         exploitation.alert_key(first[0]))
        # the owner empties the directory: the next scan is complete and writes
        # the lifting line, though nothing was evicted
        for path in many:
            os.unlink(path)
        report = menage.run_pass(self.mine, self.db, self.pol, host="pc",
                                 actor="t")
        resolved = [e for e in report["entries"]
                    if (e["kind"], e["action"]) == ("tmp", "resolved")]
        self.assertEqual(len(resolved), 1)
        self.assertIn("truncated", resolved[0]["detail"])
        self.assertEqual(self._alerts("tmp_unscanned"), [])

    # -- point 5 (#718) ---------------------------------------------------

    def test_mount_table_unreadable_is_journaled_and_alerted(self):
        tmp = menage.turn_env(self.mine, self.pol, "codex3", {})["TMPDIR"]
        ours = os.path.join(tmp, "out.txt")
        _write(ours)
        with mock.patch.object(platform, "mountinfo", return_value=[]):
            report = menage.run_pass(self.mine, self.db, self.pol, host="pc",
                                     actor="t")
        self.assertTrue(os.path.exists(ours))
        rows = [e for e in report["entries"] if e["action"] == "mounts_unreadable"]
        self.assertEqual(len(rows), 1, report["entries"])
        self.assertIn("mount table unreadable", rows[0]["detail"])
        self.assertTrue(rows[0]["data"]["mounts_unreadable"])
        # the fail-closed pass is said by a journal entry AND an alert
        alerts = self._alerts("tmp_unscanned")
        self.assertEqual(len(alerts), 1)
        self.assertIn("mount table unreadable", alerts[0]["detail"])
        self.assertEqual(self._alerts("tmp_unremovable"), [])

    def test_mount_table_unreadable_never_resolves_an_unremovable_episode(self):
        # quota 2 blocks, 3 foreign blocks: nothing can be evicted and the
        # quota is held by bytes the pass cannot remove -> a blocked
        # `unremovable` episode alerts
        pol = menage.Policy(tmp_quota=2 * B)
        tmp = menage.turn_env(self.mine, pol, "codex3", {})["TMPDIR"]
        foreign = {os.path.join(tmp, "wine", "f%d" % i) for i in range(3)}
        for path in foreign:
            _write(path)
        with _Foreign(foreign):
            menage.run_pass(self.mine, self.db, pol, host="pc", actor="t")
        first = self._alerts("tmp_unremovable")
        self.assertEqual(len(first), 1)
        with mock.patch.object(platform, "mountinfo", return_value=[]):
            report = menage.run_pass(self.mine, self.db, pol, host="pc", actor="t")
        # no `resolved` line: the pass did not scan, so the episode is not over
        self.assertEqual([e for e in report["entries"]
                          if e["action"] == "resolved"], [])
        rows = [e for e in report["entries"] if e["action"] == "mounts_unreadable"]
        self.assertEqual(len(rows), 1)
        # the alert's `since` is rounded to the millisecond by `_alert`
        self.assertEqual(round(rows[0]["data"]["episode_since"], 3), first[0]["since"])
        # the alert passes from `tmp_unremovable` to `tmp_unscanned`, carrying
        # the episode's first observation
        self.assertEqual(self._alerts("tmp_unremovable"), [])
        unscanned = self._alerts("tmp_unscanned")
        self.assertEqual(len(unscanned), 1)
        self.assertEqual(unscanned[0]["since"], first[0]["since"])

    def test_mount_table_readable_again_lifts_the_unscanned_alert(self):
        tmp = menage.turn_env(self.mine, self.pol, "codex3", {})["TMPDIR"]
        ours = os.path.join(tmp, "out.txt")
        _write(ours)
        with mock.patch.object(platform, "mountinfo", return_value=[]):
            menage.run_pass(self.mine, self.db, self.pol, host="pc", actor="t")
        self.assertEqual(len(self._alerts("tmp_unscanned")), 1)
        # the table is readable again and the directory fits the quota: the
        # episode is lifted although nothing was evicted
        report = menage.run_pass(self.mine, self.db, self.pol, host="pc",
                                 actor="t")
        resolved = [e for e in report["entries"]
                    if (e["kind"], e["action"]) == ("tmp", "resolved")]
        self.assertEqual(len(resolved), 1)
        self.assertIn("mounts_unreadable", resolved[0]["detail"])
        self.assertEqual(self._alerts("tmp_unscanned"), [])
