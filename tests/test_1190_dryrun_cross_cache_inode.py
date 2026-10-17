# SPDX-License-Identifier: AGPL-3.0-only
"""#1190: a dry-run credits an inode shared by cache directories once."""
import os
import tempfile
import unittest

from . import support  # isolate the environment before importing housekeeping
from ameesh import housekeeping


class CrossCacheInodeTest(unittest.TestCase):
    def test_dry_run_matches_apply_with_unscanned_link(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse), \
                    tempfile.TemporaryDirectory(prefix="t1190-unscanned-") as root:
                housekeeping._ensure_root(root)
                directories = [os.path.join(root, name) for name in ("npm", "pnpm")]
                other = os.path.join(root, "other")
                for directory in directories + [other]:
                    os.mkdir(directory)
                first = os.path.join(directories[0], "a")
                second = os.path.join(directories[1], "b")
                unseen = os.path.join(other, "c")
                with open(first, "wb") as stream:
                    stream.write(b"x" * 4096)
                os.utime(first, (100, 100))
                os.link(first, second)
                os.link(first, unseen)
                size = housekeeping._used_bytes(os.stat(first))
                newer = os.path.join(directories[0], "newer")
                with open(newer, "wb") as stream:
                    stream.write(b"y" * 4096)
                os.utime(newer, (200, 200))
                quota = housekeeping._used_bytes(os.stat(newer))
                if reverse:
                    directories.reverse()
                dry = housekeeping.evict_lru(
                    directories, root, quota, now=10000, dry_run=True)
                self.assertTrue(os.path.exists(first))
                self.assertTrue(os.path.exists(second))
                applied = housekeeping.evict_lru(directories, root, quota, now=10000)
                self.assertEqual(applied["files"], 1)
                self.assertEqual(applied["evicted"], 0)
                self.assertEqual(applied["unremovable"], size)
                self.assertFalse(applied["blocked"])
                self.assertEqual(dry, applied)
                self.assertFalse(os.path.exists(first))
                self.assertFalse(os.path.exists(second))
                self.assertTrue(os.path.exists(unseen))
                self.assertTrue(os.path.exists(newer))

    def test_dry_run_matches_apply_across_cache_directories(self):
        for reverse in (False, True):
            for keep_newer in (False, True):
                with self.subTest(reverse=reverse, keep_newer=keep_newer), \
                        tempfile.TemporaryDirectory(prefix="t1190-") as root:
                    housekeeping._ensure_root(root)
                    directories = [os.path.join(root, name) for name in ("npm", "pnpm")]
                    for directory in directories:
                        os.mkdir(directory)
                    first = os.path.join(directories[0], "a")
                    second = os.path.join(directories[1], "b")
                    with open(first, "wb") as stream:
                        stream.write(b"x" * 4096)
                    os.utime(first, (100, 100))
                    os.link(first, second)
                    size = housekeeping._used_bytes(os.stat(first))
                    newer = os.path.join(directories[0], "newer")
                    if keep_newer:
                        with open(newer, "wb") as stream:
                            stream.write(b"y" * 4096)
                        os.utime(newer, (200, 200))
                    quota = housekeeping._used_bytes(os.stat(newer)) if keep_newer else 0
                    if reverse:
                        directories.reverse()
                    dry = housekeeping.evict_lru(
                        directories, root, quota, now=10000, dry_run=True)
                    self.assertTrue(os.path.exists(first))
                    self.assertTrue(os.path.exists(second))
                    self.assertEqual(dry["evicted"], size)
                    self.assertEqual(dry["files"], 1)
                    applied = housekeeping.evict_lru(directories, root, quota, now=10000)
                    self.assertEqual(dry, applied)
                    self.assertFalse(os.path.exists(first))
                    self.assertFalse(os.path.exists(second))
                    if keep_newer:
                        self.assertTrue(os.path.exists(newer))
