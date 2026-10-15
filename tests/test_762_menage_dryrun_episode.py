# SPDX-License-Identifier: AGPL-3.0-only
"""#762: the two leftover P3 findings of the post-merge codex review of #550 (#530).

1. the `menage` dry-run counts as unremovable the names of an inode it would
   actually remove, and reports a blocked pass while the real pass evicts the
   block (`housekeeping.py`, verdict P2);
2. when the mount table is readable again but the listing stops at the walk
   limit, the `tmp_unscanned` episode restarts with a new `since` instead of
   continuing the one opened by the `mounts_unreadable` pass
   (`housekeeping.py`, `tmp_episode`).

The third point — the hard-link credit test that also passed on the old code —
is fixed in `test_530_housekeeping_followups.py`, where that test lives.
"""
from __future__ import annotations

import contextlib
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


#: the real `os.scandir`, captured before `DryRunTest.setUp` patches it: the
#: helper below must call it, not the patched attribute (infinite recursion).
_REAL_SCANDIR = os.scandir


@contextlib.contextmanager
def _sorted_scandir(path: str):
    """`os.scandir`, entries sorted by name: a deterministic walk order.

    #762 N2 (bureau/Claude review): `_files` walks with a LIFO stack, so the
    effective order is the reverse of what `os.scandir` returns — creation
    order on tmpfs, hash order on ext4. Pinning it sorted keeps a dry-run
    against apply comparison from depending on the machine (canon §3: a test
    that depends on the filesystem is a defect of the test, fixed at source).
    """
    with _REAL_SCANDIR(path) as it:
        yield iter(sorted(it, key=lambda entry: entry.name))


class DryRunTest(unittest.TestCase):
    """Point 1: the dry-run announces what the pass would do, hard links included."""

    def setUp(self) -> None:
        self.base = tempfile.mkdtemp(prefix="t762-")
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.root = os.path.join(self.base, "tmp")
        menage._ensure_root(self.root)
        self.mine = os.path.join(self.root, "codex3")
        patcher = mock.patch("os.scandir", side_effect=_sorted_scandir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _fields(self, result: dict) -> tuple:
        return (result["files"], result["evicted"], result["unremovable"],
                result["blocked"], result["after"])

    def test_dry_run_credits_the_names_it_would_remove(self):
        # two names of the same inode, both inside the walk: the pass removes
        # both and credits the block once; the dry-run must announce the same
        # instead of counting the block as unremovable and reporting a blocked
        # pass (#762 P2)
        first = os.path.join(self.mine, "a")
        _write(first, size=5)                    # 5 bytes: one 4096 block
        os.link(first, os.path.join(self.mine, "b"))
        names = [os.path.join(self.mine, name) for name in ("a", "b")]
        dry = menage.evict_lru([self.mine], self.root, 0, dry_run=True)
        self.assertEqual(self._fields(dry), (1, B, 0, False, 0))
        self.assertTrue(all(os.path.exists(p) for p in names),
                        "a dry-run removes nothing")
        apply_ = menage.evict_lru([self.mine], self.root, 0)
        self.assertEqual(self._fields(apply_), (1, B, 0, False, 0))
        self.assertFalse(any(os.path.exists(p) for p in names))

    def test_dry_run_does_not_credit_a_name_outside_the_walk(self):
        # one name inside the walk, another outside the managed root: the inode
        # survives the pass, nothing is freed; the dry-run must predict the
        # `unremovable` accounting, not credit the block
        inside = os.path.join(self.mine, "a")
        _write(inside, size=5)
        keep = os.path.join(self.base, "keep")
        os.link(inside, keep)                    # a name outside the eviction
        dry = menage.evict_lru([self.mine], self.root, 0, dry_run=True)
        self.assertEqual(self._fields(dry), (1, 0, B, True, B))
        self.assertTrue(os.path.exists(inside))
        apply_ = menage.evict_lru([self.mine], self.root, 0)
        self.assertEqual(self._fields(apply_), (1, 0, B, True, B))
        self.assertFalse(os.path.exists(inside))
        self.assertTrue(os.path.exists(keep))

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0,
                     "root ignores the directory permissions")
    def test_dry_run_predicts_a_name_it_cannot_unlink(self):
        # one name in a writable directory, the other in a read-only one: the
        # real pass refuses the second and stops without crediting the block;
        # the dry-run must predict that refusal (same fields), not announce an
        # eviction it would not perform
        first = os.path.join(self.mine, "w", "a")
        _write(first, size=5)
        readonly = os.path.join(self.mine, "r")
        os.makedirs(readonly)
        os.link(first, os.path.join(readonly, "b"))
        os.chmod(readonly, 0o500)
        self.addCleanup(os.chmod, readonly, 0o700)
        dry = menage.evict_lru([self.mine], self.root, 0, dry_run=True)
        self.assertEqual(self._fields(dry), (0, 0, B, True, B))
        self.assertEqual(dry["failed"], [first])
        self.assertTrue(os.path.exists(first))
        apply_ = menage.evict_lru([self.mine], self.root, 0)
        self.assertEqual(self._fields(apply_), (0, 0, B, True, B))
        self.assertEqual(apply_["failed"], [first])
        self.assertTrue(os.path.exists(first))


class EpisodeTest(PgTestCase):
    """Point 2: `mounts_unreadable` -> `truncated`, a single `tmp_unscanned` episode."""

    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM housekeeping_log")
        self.mine = _Cfg(self.state, self.cfg.dsn, self.cfg.schema)
        self.pol = menage.Policy(tmp_quota=4 * B)
        registry.upsert(self.db, "codex3", harness="codex", host="pc")

    def _alerts(self, kind: str) -> list[dict]:
        return [a for a in exploitation._housekeeping_alerts([], self.db)
                if a["type"] == kind]

    def test_truncated_after_mounts_unreadable_keeps_its_since(self):
        tmp = menage.turn_env(self.mine, self.pol, "codex3", {})["TMPDIR"]
        many = [os.path.join(tmp, "many", "f%d" % i) for i in range(6)]
        for path in many:
            _write(path)
        with mock.patch.object(platform, "mountinfo", return_value=[]):
            menage.run_pass(self.mine, self.db, self.pol, host="pc", actor="t")
        first = self._alerts("tmp_unscanned")
        self.assertEqual(len(first), 1)
        self.assertIn("mount table unreadable", first[0]["detail"])
        time.sleep(1.1)                    # a later `at_ts`, the same episode
        with mock.patch.object(menage, "WALK_LIMIT", 3):
            report = menage.run_pass(self.mine, self.db, self.pol, host="pc",
                                     actor="t")
        second = self._alerts("tmp_unscanned")
        self.assertEqual(len(second), 1)
        self.assertIn("stopped at the file limit", second[0]["detail"])
        self.assertEqual(second[0]["since"], first[0]["since"])
        self.assertEqual(exploitation.alert_key(second[0]),
                         exploitation.alert_key(first[0]))
        rows = [e for e in report["entries"] if e["action"] == "truncated"]
        self.assertEqual(len(rows), 1)
        # the alert's `since` is rounded to the millisecond by `_alert`
        self.assertEqual(round(rows[0]["data"]["episode_since"], 3),
                         first[0]["since"])
        # the listing is complete again: the episode is lifted, still under its
        # first observation
        for path in many:
            os.unlink(path)
        report = menage.run_pass(self.mine, self.db, self.pol, host="pc",
                                 actor="t")
        resolved = [e for e in report["entries"]
                    if (e["kind"], e["action"]) == ("tmp", "resolved")]
        self.assertEqual(len(resolved), 1)
        self.assertIn("truncated", resolved[0]["detail"])
        self.assertEqual(round(resolved[0]["data"]["episode_since"], 3),
                         first[0]["since"])
        self.assertEqual(self._alerts("tmp_unscanned"), [])
